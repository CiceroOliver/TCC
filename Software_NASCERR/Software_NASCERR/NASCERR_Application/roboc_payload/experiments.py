from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nascerr_modules.nascerr_operation import Operation
from nascerr_modules.nascerr_system import REQUEST_EXP
from nascerr_modules.nascerr_command import CmdTypeDef, NASCERR_MODE, W_DataTypeDef

from app.models import (
    ExperimentCommand,
    ExperimentResult,
    ExperimentResultBatch,
)

from nascerr_logging.mylogger import get_logger
from nascerr_logging.experiment_debug_logger import ExperimentDebugLogger

experiment_logger = get_logger("experiment")

EXPSUM_STATUS_CHANGE_TIMEOUT = 60.0
MAX_CRC_RETRIES = 10
# Ciclos consecutivos em P4 sem TX_RESULT/FAIL/NO_REQUEST antes de forçar reset.
# Ajuste conforme o intervalo de polling do loop principal.
MAX_P4_STALL_CYCLES = 200

# Máximo de offsets possíveis no expsum (0, 30, 60, ..., 210) = 8 janelas
MAX_SUMMARY_OFFSET = 210

# Campos do expstatus que indicam atividade no hardware (scheduler ou experimentos)
_ACTIVITY_FIELDS = (
    "qtd_scheduler_total",
    "qtd_scheduler_executed",
    "qtd_experiments_total",
    "qtd_experiments_finished",
)

@dataclass
class _ReaderState:
    """Estado interno do leitor de experimentos."""
    exp_flash_page: int = 0
    summary_page: int = -1
    summary_entries: Optional[List[Dict[str, Any]]] = None
    summary_changed: bool = False
    last_qtd_total: int = 0
    last_qtd_finished: int = 0
    last_was_active: bool = False  # True se qualquer campo de atividade era > 0 no ciclo anterior
    scan_offset: int = 0
    requested: bool = False
    await_summary_confirm: bool = False
    # attempts: int = 0
    requested_at: float = 0.0
    await_finished: bool = False

    # Controle de varredura completa sem NOT_READ
    # scan_started_at_page: int = -1   # flash_page no início da varredura atual
    offsets_scanned: int = 0         # quantos offsets foram varridos neste ciclo

    # Id do experimento atual sendo lido (preenchido em _request_page)
    current_exp_id: int = 0

    # Retentativas por CRC inválido
    crc_cmd_retry_count: int = 0   # quantos fluxos READ_CMD completos já foram refeitos

    # Rastreamento da página atualmente em leitura — permite detectar quando
    # o sumário avança para a próxima página sem que tenhamos recebido um
    # resultado válido nem disparado a perda permanente (perda silenciosa).
    pending_attempt_page: int = -1
    pending_attempt_exp_id: int = 0
    pending_attempt_resolved: bool = True

    # Fila de resultados-sentinela (perda de página) aguardando envio ao servidor
    pending_lost_results: List[ExperimentResult] = field(default_factory=list)

    # Detecção de travamento em P4
    p4_stall_count: int = 0        # ciclos consecutivos sem mudança de estado no HW

    def phase_label(self) -> str:
        """Retorna label legível do estado atual da máquina."""
        if self.await_summary_confirm:
            return "AGUARDANDO_CONFIRM_SUMARIO"
        if self.requested:
            return "AGUARDANDO_HW_PRONTO"
        return "IDLE"

    @property
    def max_offsets(self) -> int:
        """Número total de janelas de offset possíveis (0, 30, ..., 210)."""
        return (MAX_SUMMARY_OFFSET // 30) + 1  # = 8


class HardwareExperimentService:
    """
    Camada de hardware para experimentos:
    - Despachar comandos (WRITE_CMD)
    - Detectar e coletar resultados finalizados (READ_CMD + read_experiment)
    - Gerenciar sumário e paginação global corretamente

    Fluxo da máquina de estados (read_next):
        IDLE
          │ unread > 0
          ▼
        [P4] _request_page → busca NOT_READ no expsum
          │ encontrou → READ_CMD enviado
          │                    └─► AGUARDANDO_HW_PRONTO
          │                              │ REQ_EXP_TX_RESULT
          │                              ▼
          │                        _read_buffer → AGUARDANDO_CONFIRM_SUMARIO
          │                                              │ status==READ
          │                                              ▼
          │                                            IDLE (próxima página)
    """

    ENTRIES_PER_SUMMARY_PAGE = 30

    def __init__(
        self,
        op: Operation,
        hw_lock: threading.Lock,
        default_ecc: NASCERR_MODE = NASCERR_MODE.MRSC,
        default_write_type: W_DataTypeDef = W_DataTypeDef.WRITE_ZEROS,
        debug_log_dir: str = "logs/experiment_debug",
    ) -> None:
        self._op = op
        self._hw_lock = hw_lock
        self._default_ecc = default_ecc
        self._default_write_type = default_write_type
        self._state = _ReaderState()
        self._dbg = ExperimentDebugLogger(log_dir=debug_log_dir)
        self._cycle_no: int = 0
        self._watchdog_reset_occurred: bool = False

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def dispatch(self, cmd: ExperimentCommand) -> None:
        """Envia um comando WRITE_CMD para o hardware."""
        with self._hw_lock:
            res = self._op.send_commandNew(
                cmd_type=CmdTypeDef.WRITE_CMD,
                id=cmd.id,
                block=cmd.block,
                qtd_block=cmd.qtd_block,
                ecc=NASCERR_MODE(int(cmd.ecc)),
                write_type=W_DataTypeDef(int(cmd.write_type)),
                scheduler_time=cmd.scheduler_time,
                repeat=cmd.repeat,
                delay=cmd.delay,
                current_tick=1,
            )
        if not res or res.get("error"):
            raise RuntimeError(
                res.get("log_error", "Falha ao despachar experimento"))

    def read_next(self) -> Optional[ExperimentResultBatch]:
        """Máquina de estados para leitura de experimentos."""
        self._cycle_no += 1
        self._dbg.cycle_start(self._cycle_no, self._state_dict())
        try:
            return self._do_read_next()
        finally:
            self._dbg.cycle_end()

    def _do_read_next(self) -> Optional[ExperimentResultBatch]:
        """Corpo da máquina de estados — chamado por read_next."""
        st = self._state

        # ══════════════════════════════════════════════════════════════
        # [PASSO 1] Consulta expstatus
        # ══════════════════════════════════════════════════════════════
        experiment_logger.debug(
            f"[P1:EXPSTATUS] Consultando hardware | fase_atual={st.phase_label()}"
        )
        status = self._get_experiment_status()
        if status is None:
            experiment_logger.warning(
                "[P1:EXPSTATUS] FALHA — expstatus retornou None. "
                "Conexão com hardware pode estar comprometida."
            )
            self._dbg.hw_status_fail()
            return None

        qtd_unread = status.get("qtd_experiments_unread", 0)
        qtd_finished = status.get("qtd_experiments_finished", 0)

        experiment_logger.debug(
            f"[P1:EXPSTATUS] OK "
            f"| fase={st.phase_label()} "
            f"| unread={qtd_unread} finished={qtd_finished} "
            f"| flash_page={st.exp_flash_page} "
            f"| total={status.get('qtd_experiments_total', 0)} "
            f"| running={status.get('qtd_experiments_running', 0)} "
            f"| pending={status.get('qtd_scheduler_pending', 0)} "
            f"| sch_executed={status.get('qtd_scheduler_executed', 0)} "
            f"| requested={st.requested} await_summary={st.await_summary_confirm} "
            f"| offsets_scanned={st.offsets_scanned}"
        )
        self._dbg.hw_status_ok(status)

        # Guard: reset de sessão detectado — todos os campos de atividade zeraram
        hw_fully_reset = all(status.get(f, 0) == 0 for f in _ACTIVITY_FIELDS)
        if st.last_was_active and hw_fully_reset:
            experiment_logger.warning(
                f"[WATCHDOG] expstatus zerou completamente (last_qtd_total={st.last_qtd_total}) — "
                "reset do hardware detectado."
            )
            self._dbg.hw_reset_detected(st.last_qtd_total)
            self._watchdog_reset_occurred = True
            self._state = _ReaderState()
            return None

        st.last_qtd_total = status.get("qtd_experiments_total", 0)
        st.last_was_active = any(status.get(f, 0) > 0 for f in _ACTIVITY_FIELDS)

        # ══════════════════════════════════════════════════════════════
        # [PASSO 1.5] Drena fila de resultados-sentinela (páginas perdidas)
        # ══════════════════════════════════════════════════════════════
        # Garante que toda página abandonada — por perda permanente OU por
        # ter sido silenciosamente pulada pelo sumário — chegue ao servidor,
        # de forma que current_total sempre alcance expected_total.
        if st.pending_lost_results:
            lost = st.pending_lost_results.pop(0)
            experiment_logger.warning(
                f"[SENTINEL] Emitindo resultado de perda para pág {lost.block} "
                f"(exp_id={lost.experiment_id}, motivo={lost.mdca_data.get('error')}) "
                f"| {len(st.pending_lost_results)} pendente(s) na fila."
            )
            return ExperimentResultBatch(device_name="", results=[lost])

        # ══════════════════════════════════════════════════════════════
        # [PASSO 2] Confirmação de leitura pelo sumário
        # ══════════════════════════════════════════════════════════════
        if st.await_summary_confirm:
            page_just_read = st.exp_flash_page - 1

            if qtd_unread == 0:
                # Hardware zerou unread — confirmação implícita, não precisa checar sumário.
                experiment_logger.info(
                    f"[P2:CONFIRM_SUMARIO] qtd_unread=0 — confirmação implícita "
                    f"para pág {page_just_read}. Liberando."
                )
                st.await_summary_confirm = False
                st.offsets_scanned = 0
                return None

            elapsed = time.time() - st.requested_at
            experiment_logger.debug(
                f"[P2:CONFIRM_SUMARIO] Verificando se pág {page_just_read} foi marcada READ "
                f"| elapsed={elapsed:.1f}s / timeout={EXPSUM_STATUS_CHANGE_TIMEOUT}s"
            )
            if self._check_summary_marked_read():
                experiment_logger.info(
                    f"[P2:CONFIRM_SUMARIO] ✓ Pág {page_just_read} confirmada como READ. "
                    f"Liberando para próxima leitura."
                )
                st.await_summary_confirm = False
                st.offsets_scanned = 0
            else:
                experiment_logger.debug(
                    f"[P2:CONFIRM_SUMARIO] Aguardando... "
                    f"pág {page_just_read} ainda NOT_READ no sumário."
                )
            return None

        # ══════════════════════════════════════════════════════════════
        # [PASSO 4] READ_CMD já enviado — polling do hardware
        # ══════════════════════════════════════════════════════════════
        if st.requested:
            elapsed = time.time() - st.requested_at
            experiment_logger.debug(
                f"[P4:POLLING_HW] Verificando status | pág={st.exp_flash_page} "
                f"elapsed={elapsed:.1f}s"
            )
            is_ready, is_fail, req_name = self._poll_hw_exp_status()

            if is_ready:
                st.p4_stall_count = 0
                experiment_logger.info(
                    f"[P4:POLLING_HW] ✓ Hardware PRONTO (REQ_EXP_TX_RESULT). "
                    f"Iniciando leitura do buffer para pág {st.exp_flash_page}..."
                )
                self._dbg.hw_ready(st.exp_flash_page)
                batch = self._read_buffer()
                if batch:
                    experiment_logger.info(
                        f"[P4:POLLING_HW] ✓ Buffer lido — {len(batch.results)} resultado(s). "
                        f"Próxima pág={st.exp_flash_page}. "
                        f"Aguardando confirmação do sumário..."
                    )
                    st.await_summary_confirm = True
                    st.requested_at = time.time()
                    return batch
                else:
                    experiment_logger.warning(
                        f"[P4:POLLING_HW] Hardware PRONTO mas _read_buffer retornou vazio/None. "
                        f"requested resetado — tentará novo ciclo."
                    )
            elif is_fail:
                st.p4_stall_count = 0
                experiment_logger.warning(
                    f"[P4:POLLING_HW] ✗ Falha detectada pelo hardware: {req_name} "
                    f"| pág={st.exp_flash_page} — resetando requested."
                )
                self._dbg.request_timeout(st.exp_flash_page, elapsed)
                st.requested = False
            else:
                st.p4_stall_count += 1
                self._dbg.hw_not_ready(st.exp_flash_page, elapsed)
                experiment_logger.debug(
                    f"[P4:POLLING_HW] Hardware processando | req={req_name} "
                    f"elapsed={elapsed:.1f}s "
                    f"| stall={st.p4_stall_count}/{MAX_P4_STALL_CYCLES}"
                )
                if st.p4_stall_count >= MAX_P4_STALL_CYCLES:
                    experiment_logger.warning(
                        f"[P4:POLLING_HW] ✗ Hardware travado em '{req_name}' por "
                        f"{st.p4_stall_count} ciclos | pág={st.exp_flash_page}. "
                        f"Forçando reset de requested."
                    )
                    self._dbg.request_timeout(st.exp_flash_page, elapsed)
                    st.p4_stall_count = 0
                    st.requested = False
            return None

        # ══════════════════════════════════════════════════════════════
        # [PASSO 4.5] Última página pendente nunca resolvida e nada mais
        # a ler — o hardware zerou unread sem confirmar essa leitura
        # (perda silenciosa da última página do experimento).
        # ══════════════════════════════════════════════════════════════
        if (qtd_unread == 0
                and not st.requested
                and not st.await_summary_confirm
                and not st.pending_attempt_resolved
                and st.pending_attempt_page != -1):
            experiment_logger.error(
                f"[P4.5:PENDING_CHECK] ✗ Pág {st.pending_attempt_page} "
                f"(exp_id={st.pending_attempt_exp_id}) nunca foi confirmada e "
                f"unread chegou a 0 — perda silenciosa da última página. "
                f"Emitindo sentinela de perda."
            )
            self._dbg.page_permanently_lost(st.pending_attempt_page, 0)
            st.pending_lost_results.append(
                ExperimentResult(
                    experiment_id=st.pending_attempt_exp_id,
                    block=st.pending_attempt_page,
                    percent_error=100.0,
                    number_errors=0xFFFFFFFF,  # uint32 — sentinel "invalido" (-1 nao cabe em uint32, derruba o gRPC)
                    mdca_data={"error": "FLASH_CRC_SILENT_SKIP"},
                )
            )
            st.pending_attempt_resolved = True
            return None

        # ══════════════════════════════════════════════════════════════
        # [PASSO 5] IDLE — solicitar próxima página não lida
        # ══════════════════════════════════════════════════════════════
        if qtd_unread > 0 and not st.requested and not st.await_summary_confirm:
            experiment_logger.debug(
                f"[P5:REQUEST_PAGE] {qtd_unread} experimento(s) não lido(s). "
                f"Consultando sumário a partir de flash_page={st.exp_flash_page} "
                f"| offsets_scanned={st.offsets_scanned}/{st.max_offsets}"
            )

            if st.offsets_scanned >= st.max_offsets:
                experiment_logger.warning(
                    f"[P5:REQUEST_PAGE] Varredura completa ({st.offsets_scanned} offsets) "
                    f"sem encontrar NOT_READ, mas expstatus indica unread={qtd_unread}. "
                    f"Reiniciando varredura — flash limpa automaticamente."
                )
                st.offsets_scanned = 0
                st.scan_offset = 0
                return None

            if self._request_page():
                experiment_logger.info(
                    f"[P5:REQUEST_PAGE] ✓ READ_CMD enviado para pág {st.exp_flash_page}. "
                    f"Entrando em fase AGUARDANDO_HW_PRONTO."
                )
                st.requested = True
                st.requested_at = time.time()
                st.offsets_scanned = 0   # encontrou — reset da contagem
                st.p4_stall_count = 0    # novo ciclo de polling, zera o stall
            else:
                experiment_logger.warning(
                    f"[P5:REQUEST_PAGE] ✗ _request_page retornou False "
                    f"| flash_page={st.exp_flash_page} "
                    f"| summary_page={st.summary_page} "
                    f"| entries_count={len(st.summary_entries) if st.summary_entries else 0} "
                    f"| offsets_scanned até agora={st.offsets_scanned}"
                )
            return None

        experiment_logger.debug(
            f"[FIM_CICLO] Nada a fazer "
            f"| unread={qtd_unread} requested={st.requested} "
            f"| await_summary={st.await_summary_confirm}"
        )
        return None

    def _load_summary_by_offset(self, offset: int) -> bool:
        """Carrega o sumário de um offset específico (0, 30, 60, ..., 210)."""
        experiment_logger.debug(
            f"[LOAD_SUMMARY] Setando offset={offset} no hardware"
        )
        with self._hw_lock:
            resp_idx = self._op.set_exp_sum_idx(offset)
            if not resp_idx or resp_idx.get("error"):
                experiment_logger.error(
                    f"[LOAD_SUMMARY] ✗ Falha ao setar offset={offset}: {resp_idx}"
                )
                self._dbg.summary_load_fail(offset, "falha ao setar offset")
                return False

        time.sleep(0.1)

        with self._hw_lock:
            resp = self._op.get_exp_sum()
            if not resp or resp.get("error"):
                experiment_logger.error(
                    f"[LOAD_SUMMARY] ✗ Falha ao ler expsum: {resp}"
                )
                self._dbg.summary_load_fail(offset, "falha ao ler expsum")
                return False

        entries = resp.get("data", {}).get("entries", [])
        if not entries:
            experiment_logger.warning(
                f"[LOAD_SUMMARY] entries vazio para offset={offset}."
            )
            self._dbg.summary_load_fail(offset, "entries vazio")
            return False

        self._state.summary_entries = entries
        self._state.summary_page = offset // self.ENTRIES_PER_SUMMARY_PAGE

        not_read = sum(1 for e in entries if e.get("status") == "NOT_READ")
        read = sum(1 for e in entries if e.get("status") == "READ")
        experiment_logger.debug(
            f"[LOAD_SUMMARY] ✓ offset={offset} summary_page={self._state.summary_page} "
            f"| total={len(entries)} NOT_READ={not_read} READ={read}"
        )
        self._dbg.summary_loaded(offset, self._state.summary_page, entries)
        return True

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _check_summary_marked_read(self) -> bool:
        st = self._state
        page_just_read = st.exp_flash_page - 1

        experiment_logger.debug(
            f"[SUMARIO] Carregando sumário para pág {page_just_read} "
            f"| entry_idx esperado={page_just_read % self.ENTRIES_PER_SUMMARY_PAGE}"
        )
        self._load_summary(page_just_read)

        entry_idx = page_just_read % self.ENTRIES_PER_SUMMARY_PAGE
        entry_status = "unknown"

        if not st.summary_entries:
            experiment_logger.warning(
                f"[SUMARIO] summary_entries vazio após _load_summary. "
                f"Não foi possível verificar pág {page_just_read}."
            )
        elif entry_idx < len(st.summary_entries):
            entry = st.summary_entries[entry_idx]
            status_val = entry.get("status")
            entry_status = status_val or "null"
            experiment_logger.debug(
                f"[SUMARIO] entry[{entry_idx}] "
                f"| id={entry.get('id')} status={status_val} "
                f"| summary_page={st.summary_page}"
            )
            if status_val == "READ":
                self._dbg.confirm_read(page_just_read)
                return True
        else:
            experiment_logger.warning(
                f"[SUMARIO] entry_idx={entry_idx} fora dos limites "
                f"| entries disponíveis={len(st.summary_entries)}"
            )

        elapsed = time.time() - st.requested_at
        if elapsed > EXPSUM_STATUS_CHANGE_TIMEOUT:
            experiment_logger.warning(
                f"[SUMARIO] TIMEOUT ({elapsed:.1f}s) aguardando READ na pág {page_just_read}. "
                f"Forçando saída de await_summary_confirm."
            )
            self._dbg.confirm_timeout(page_just_read, elapsed)
            st.await_summary_confirm = False
        else:
            self._dbg.confirm_waiting(page_just_read, entry_status, elapsed)

        return False

    def _get_experiment_status(self) -> Optional[Dict[str, Any]]:
        with self._hw_lock:
            hw_st = self._op.get_experiment_status()
        if not hw_st or hw_st.get("error") or not hw_st.get("data"):
            experiment_logger.error(
                f"[HW] get_experiment_status retornou erro ou None: {hw_st}"
            )
            return None
        return hw_st["data"]

    def was_watchdog_reset(self) -> bool:
        """Retorna True (e limpa a flag) se um reset de hardware foi detectado desde a última chamada."""
        if self._watchdog_reset_occurred:
            self._watchdog_reset_occurred = False
            return True
        return False

    def _reset_state(self) -> None:
        experiment_logger.info("[STATE] Reset completo do estado interno.")
        self._state = _ReaderState()

    def _poll_hw_exp_status(self) -> tuple:
        """
        Faz uma única chamada a get_status() e retorna (is_ready, is_fail, req_name).

        is_ready  — hardware sinalizou REQ_EXP_TX_RESULT (dado pronto para leitura)
        is_fail   — hardware sinalizou REQ_EXP_FAIL ou REQ_EXP_NO_REQUEST
        req_name  — nome legível do estado atual para logging
        """
        with self._hw_lock:
            hw_st = self._op.get_status()

        if not hw_st or hw_st.get("error"):
            experiment_logger.warning(
                f"[HW_STATUS] get_status retornou erro ou None: {hw_st}"
            )
            return False, False, "erro_get_status"

        try:
            req_val  = hw_st["data"]["request"]["EXP"]["value"]
            req_name = hw_st["data"]["request"]["EXP"]["name"]
        except (KeyError, TypeError) as e:
            experiment_logger.warning(
                f"[HW_STATUS] Erro ao parsear request.EXP: {e} | raw={hw_st}"
            )
            return False, False, "parse_error"

        is_ready = req_val == REQUEST_EXP.REQ_EXP_TX_RESULT.value
        is_fail  = req_val in (
            REQUEST_EXP.REQ_EXP_FAIL.value,
            REQUEST_EXP.REQ_EXP_NO_REQUEST.value,
        )
        experiment_logger.debug(
            f"[HW_STATUS] request.EXP={req_name} (value={req_val}) "
            f"| pronto={is_ready} falha={is_fail}"
        )
        return is_ready, is_fail, req_name

    def _request_page(self) -> bool:
        st = self._state

        experiment_logger.debug(
            f"[REQUEST_PAGE] Carregando sumário "
            f"| scan_offset={st.scan_offset} offsets_scanned={st.offsets_scanned}"
        )
        self._dbg.offset_scan(st.scan_offset, st.offsets_scanned, st.max_offsets)

        if not self._load_summary_by_offset(st.scan_offset):
            experiment_logger.warning(
                f"[REQUEST_PAGE] Offset {st.scan_offset} inválido. Avançando."
            )
            st.scan_offset = (st.scan_offset + 30) % (MAX_SUMMARY_OFFSET + 30)
            st.offsets_scanned += 1
            return False

        for i, entry in enumerate(st.summary_entries or []):
            experiment_logger.debug(
                f"[REQUEST_PAGE]   entry[{i:02d}] "
                f"id={entry.get('id')} status={entry.get('status')}"
            )

        target_entry = None
        found_page = -1
        found_entry_idx = -1
        for i, entry in enumerate(st.summary_entries or []):
            if entry.get("status") == "NOT_READ":
                target_entry = entry
                found_page = (st.summary_page *
                              self.ENTRIES_PER_SUMMARY_PAGE) + i
                found_entry_idx = i
                break

        if not target_entry:
            next_scan_offset = (
                st.scan_offset + 30) % (MAX_SUMMARY_OFFSET + 30)
            experiment_logger.warning(
                f"[REQUEST_PAGE] Sem NOT_READ no offset {st.scan_offset}. "
                f"Avançando scan_offset: {st.scan_offset} → {next_scan_offset} "
                f"| offsets_scanned: {st.offsets_scanned} → {st.offsets_scanned + 1}"
            )
            self._dbg.offset_no_not_read(
                st.scan_offset, next_scan_offset, st.offsets_scanned + 1
            )
            st.scan_offset = next_scan_offset
            st.offsets_scanned += 1
            return False

        original_offset = st.scan_offset
        id_exp = int(target_entry["id"])

        # ── Detecção de perda silenciosa ────────────────────────────
        # Se a página que estávamos tentando ler ainda não foi resolvida
        # (nem sucesso, nem perda permanente já reportada) e o sumário
        # agora aponta para uma página diferente, o hardware avançou seu
        # buffer e descartou aquela página sem nunca confirmar a leitura.
        # Isso acontece em falhas de CRC isoladas, que resetam o contador
        # de retry antes de atingir MAX_CRC_RETRIES — sem essa checagem
        # o resultado simplesmente desaparece e current_total nunca
        # alcança expected_total no servidor.
        if (not st.pending_attempt_resolved
                and st.pending_attempt_page != -1
                and st.pending_attempt_page != found_page):
            experiment_logger.error(
                f"[REQUEST_PAGE] ✗ Pág {st.pending_attempt_page} "
                f"(exp_id={st.pending_attempt_exp_id}) foi abandonada silenciosamente "
                f"— sumário avançou para pág {found_page} sem confirmar a leitura. "
                f"Emitindo sentinela de perda."
            )
            self._dbg.page_permanently_lost(st.pending_attempt_page, 0)
            st.pending_lost_results.append(
                ExperimentResult(
                    experiment_id=st.pending_attempt_exp_id,
                    block=st.pending_attempt_page,
                    percent_error=100.0,
                    number_errors=0xFFFFFFFF,  # uint32 — sentinel "invalido" (-1 nao cabe em uint32, derruba o gRPC)
                    mdca_data={"error": "FLASH_CRC_SILENT_SKIP"},
                )
            )

        st.exp_flash_page = found_page
        st.scan_offset = 0          # reset para próxima varredura começar do zero
        st.offsets_scanned = 0
        st.current_exp_id = id_exp
        st.pending_attempt_page = found_page
        st.pending_attempt_exp_id = id_exp
        st.pending_attempt_resolved = False

        self._dbg.offset_found(original_offset, found_entry_idx, id_exp, found_page)

        experiment_logger.info(
            f"[REQUEST_PAGE] NOT_READ encontrado "
            f"| offset={original_offset} entry id={id_exp} → flash_page={found_page}"
        )
        with self._hw_lock:
            res = self._op.send_commandNew(
                cmd_type=CmdTypeDef.READ_CMD,
                id=id_exp,
                block=st.exp_flash_page,
                qtd_block=0,
                ecc=self._default_ecc,
                write_type=self._default_write_type,
                scheduler_time=0,
                repeat=0,
                delay=0,
                current_tick=0,
            )

        success = bool(res and not res.get("error"))
        experiment_logger.info(
            f"[REQUEST_PAGE] {'✓ READ_CMD aceito' if success else '✗ READ_CMD rejeitado'} "
            f"| id_exp={id_exp} flash_page={st.exp_flash_page} | resposta={res}"
        )
        if success:
            self._dbg.request_sent(st.exp_flash_page, id_exp)
        else:
            self._dbg.request_rejected(st.exp_flash_page, id_exp, res)
        return success

    def _read_buffer(self) -> Optional[ExperimentResultBatch]:
        """Lê o buffer de experimento e avança o estado."""
        current_page = self._state.exp_flash_page
        experiment_logger.debug(
            f"[READ_BUFFER] Chamando read_experiment() "
            f"| flash_page={current_page}"
        )
        with self._hw_lock:
            r = self._op.read_experiment()

        if not r or r.get("error"):
            log_error = (r.get("log_error", "") or "") if r else ""
            is_crc = "CRC" in log_error.upper()

            if not is_crc:
                experiment_logger.error(
                    f"[READ_BUFFER] ✗ read_experiment() retornou erro/None: {r}"
                )
                self._dbg.page_read_fail(current_page, f"read_experiment erro: {r}")
                self._state.crc_cmd_retry_count = 0
                self._state.requested = False
                return None

            # ── Nível 1: soft retry ─────────────────────────────────
            # CRC inválido → tenta ler o registro TXB mais uma vez sem
            # reenviar o READ_CMD, pois o dado pode ainda estar disponível.
            experiment_logger.warning(
                f"[READ_BUFFER] CRC inválido na pág {current_page} "
                f"— soft retry (releitura sem READ_CMD)..."
            )
            with self._hw_lock:
                r = self._op.read_experiment()

            if r and not r.get("error"):
                # Soft retry recuperou o dado — continua o fluxo normal abaixo
                experiment_logger.info(
                    f"[READ_BUFFER] ✓ Soft retry bem-sucedido na pág {current_page}."
                )
            else:
                # ── Nível 2: fluxo READ_CMD completo ────────────────
                # Soft retry também falhou → reenvia READ_CMD e aguarda
                # o hardware reescrever os dados no TXB.
                if self._state.crc_cmd_retry_count < MAX_CRC_RETRIES:
                    self._state.crc_cmd_retry_count += 1
                    experiment_logger.warning(
                        f"[READ_BUFFER] Soft retry falhou na pág {current_page} "
                        f"| iniciando fluxo READ_CMD completo "
                        f"({self._state.crc_cmd_retry_count}/{MAX_CRC_RETRIES})."
                    )
                    self._dbg.page_crc_retry(
                        current_page,
                        self._state.crc_cmd_retry_count,
                        MAX_CRC_RETRIES,
                    )
                    self._state.requested = False
                    return None

                experiment_logger.error(
                    f"[READ_BUFFER] ✗ Pág {current_page} permanentemente perdida "
                    f"após {self._state.crc_cmd_retry_count} fluxo(s) READ_CMD completo(s)."
                )
                self._dbg.page_permanently_lost(
                    current_page, self._state.crc_cmd_retry_count
                )
                self._state.crc_cmd_retry_count = 0
                self._state.exp_flash_page += 1
                self._state.requested = False
                self._state.pending_attempt_resolved = True
                lost_result = ExperimentResult(
                    experiment_id=self._state.current_exp_id,
                    block=current_page,
                    percent_error=100.0,
                    number_errors=0xFFFFFFFF,  # uint32 — sentinel "invalido" (-1 nao cabe em uint32, derruba o gRPC)
                    mdca_data={"error": "FLASH_CRC_PERMANENT_LOSS"},
                )
                return ExperimentResultBatch(device_name="", results=[lost_result])

        raw_data = r.get("data", [])
        experiment_logger.debug(
            f"[READ_BUFFER] raw_data recebido com {len(raw_data)} item(s)"
        )

        if not raw_data:
            experiment_logger.warning(
                "[READ_BUFFER] raw_data vazio. Resetando requested."
            )
            self._dbg.page_read_fail(current_page, "raw_data vazio")
            self._state.requested = False
            return None

        try:
            results = [
                self._map_exp_result(x)
                for x in raw_data
                if isinstance(x, dict)
            ]
            experiment_logger.debug(
                f"[READ_BUFFER] {len(results)} resultado(s) mapeados com sucesso."
            )
        except Exception as e:
            experiment_logger.error(
                f"[READ_BUFFER] ✗ Erro ao mapear resultados: {e}. Resetando requested."
            )
            self._dbg.page_read_fail(current_page, f"erro ao mapear: {e}")
            self._state.requested = False
            return None

        self._state.exp_flash_page += 1
        self._state.requested = False
        self._state.await_finished = False
        self._state.crc_cmd_retry_count = 0
        if self._state.pending_attempt_page == current_page:
            self._state.pending_attempt_resolved = True

        experiment_logger.info(
            f"[READ_BUFFER] ✓ Pág {current_page} lida com sucesso "
            f"| {len(results)} resultado(s) "
            f"| flash_page: {current_page} → {self._state.exp_flash_page}"
        )
        self._dbg.page_read_ok(current_page, len(results), self._state.exp_flash_page)
        return ExperimentResultBatch(device_name="", results=results)

    def _load_summary(self, page_idx: int) -> bool:
        """Seta o offset no hardware e lê o sumário de experimentos."""
        offset = (page_idx // self.ENTRIES_PER_SUMMARY_PAGE) * \
            self.ENTRIES_PER_SUMMARY_PAGE

        experiment_logger.debug(
            f"[LOAD_SUMMARY] Setando offset={offset} no hardware "
            f"| page_idx={page_idx}"
        )
        with self._hw_lock:
            resp_idx = self._op.set_exp_sum_idx(offset)
            if not resp_idx or resp_idx.get("error"):
                experiment_logger.error(
                    f"[LOAD_SUMMARY] ✗ Falha ao setar offset={offset}: {resp_idx}"
                )
                self._dbg.summary_load_fail(offset, "falha ao setar offset")
                return False

        time.sleep(0.1)

        experiment_logger.debug(
            f"[LOAD_SUMMARY] Lendo sumário do hardware (get_exp_sum)..."
        )
        with self._hw_lock:
            resp = self._op.get_exp_sum()
            if not resp or resp.get("error"):
                experiment_logger.error(
                    f"[LOAD_SUMMARY] ✗ Falha ao ler expsum: {resp}"
                )
                self._dbg.summary_load_fail(offset, "falha ao ler expsum")
                return False

        entries = resp.get("data", {}).get("entries", [])
        if not entries:
            experiment_logger.warning(
                f"[LOAD_SUMMARY] expsum retornou entries vazio para offset={offset}."
            )
            self._dbg.summary_load_fail(offset, "entries vazio")
            return False

        self._state.summary_entries = entries
        self._state.summary_page = offset // self.ENTRIES_PER_SUMMARY_PAGE

        not_read_count = sum(
            1 for e in entries if e.get("status") == "NOT_READ")
        read_count = sum(1 for e in entries if e.get("status") == "READ")
        experiment_logger.debug(
            f"[LOAD_SUMMARY] ✓ Sumário carregado "
            f"| offset={offset} summary_page={self._state.summary_page} "
            f"| total_entries={len(entries)} NOT_READ={not_read_count} READ={read_count}"
        )
        self._dbg.summary_loaded(offset, self._state.summary_page, entries)
        return True

    def sync_initial_state(self) -> None:
        """
        Sincroniza o estado inicial com o hardware.
        Deve ser chamado uma vez antes do loop de coleta iniciar.
        """
        experiment_logger.info(
            "[SYNC] Iniciando sincronização do estado inicial...")
        while True:
            status = self._get_experiment_status()
            if status:
                finished = int(status.get("qtd_experiments_finished", 0))
                self._state.exp_flash_page = 0
                self._state.last_qtd_finished = -1
                self._state.last_qtd_total = int(status.get("qtd_experiments_total", 0))
                self._state.last_was_active = any(status.get(f, 0) > 0 for f in _ACTIVITY_FIELDS)
                self._state.offsets_scanned = 0
                experiment_logger.info(
                    f"[SYNC] ✓ Estado sincronizado "
                    f"| qtd_finished={finished} "
                    f"| exp_flash_page=0 "
                    f"| last_qtd_total={self._state.last_qtd_total}"
                )
                return
            experiment_logger.warning(
                "[SYNC] expstatus indisponível, aguardando 100ms..."
            )
            time.sleep(0.1)

    def _map_exp_result(self, exp: Dict[str, Any]) -> ExperimentResult:
        return ExperimentResult(
            experiment_id=int(exp.get("id", 0)),
            block=int(exp.get("block", 0)),
            data_length=int(exp.get("data_lenght", exp.get("data_length", 0))),
            time_delta=int(exp.get("time_delta", 0)),
            number_errors=int(exp.get("number_errors", 0)),
            corrected_data=int(exp.get("corrected_data", 0)),
            percent_error=float(exp.get("percent_error", 0.0)),
            ecc_telemetry=exp.get("ecc_telemetry") or {},
            mdca_data=exp.get("mdca_data") or {},
            raw=exp,
        )

    def _get_ack_stage(self) -> dict:
        with self._hw_lock:
            hw_st = self._op.get_ack()
        if hw_st and not hw_st.get("error"):
            return hw_st
        experiment_logger.warning(
            f"[ACK] get_ack retornou erro ou None: {hw_st}"
        )
        return None

    def _state_dict(self) -> Dict[str, Any]:
        st = self._state
        return {
            "phase":                 st.phase_label(),
            "exp_flash_page":        st.exp_flash_page,
            "summary_page":          st.summary_page,
            "scan_offset":           st.scan_offset,
            "offsets_scanned":       st.offsets_scanned,
            "requested":             st.requested,
            "await_summary_confirm": st.await_summary_confirm,
            "crc_cmd_retry_count":   st.crc_cmd_retry_count,
            "p4_stall_count":        st.p4_stall_count,
            "last_qtd_total":        st.last_qtd_total,
            "last_qtd_finished":     st.last_qtd_finished,
            "last_was_active":       st.last_was_active,
        }
