"""
experiment_debug_logger.py
--------------------------
Logger de debug estruturado para HardwareExperimentService.

Grava um arquivo .jsonl (JSON Lines) rotativo com um evento por linha.
Cada evento tem:
  - ts          : timestamp ISO-8601
  - cycle       : contador de chamadas a read_next()
  - event       : identificador do evento (ver EventTag)
  - payload     : dict com dados do evento

Para analisar offline:

    python -c "
    import json, sys
    for line in open('experiment_debug.jsonl'):
        e = json.loads(line)
        print(e['ts'], e['event'], json.dumps(e.get('payload', {})))
    "

Para filtrar apenas saltos de página:

    grep -E '"event": "(PAGE_READ_OK|PAGE_SKIPPED|REQUEST_SENT)"' experiment_debug.jsonl
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Tags de evento — mantém busca por grep previsível
# ---------------------------------------------------------------------------

class EventTag(str, Enum):
    # Ciclo geral
    CYCLE_START         = "CYCLE_START"
    CYCLE_END           = "CYCLE_END"

    # Status do hardware
    HW_STATUS_OK        = "HW_STATUS_OK"
    HW_STATUS_FAIL      = "HW_STATUS_FAIL"
    HW_RESET_DETECTED   = "HW_RESET_DETECTED"

    # Sumário
    SUMMARY_LOADED      = "SUMMARY_LOADED"
    SUMMARY_LOAD_FAIL   = "SUMMARY_LOAD_FAIL"
    SUMMARY_ENTRIES     = "SUMMARY_ENTRIES"       # dump completo das entries

    # Varredura de offsets
    OFFSET_SCAN         = "OFFSET_SCAN"
    OFFSET_NO_NOT_READ  = "OFFSET_NO_NOT_READ"
    OFFSET_FOUND        = "OFFSET_FOUND"

    # Requisição de página
    REQUEST_SENT        = "REQUEST_SENT"
    REQUEST_REJECTED    = "REQUEST_REJECTED"
    REQUEST_TIMEOUT     = "REQUEST_TIMEOUT"

    # Leitura de buffer
    HW_READY            = "HW_READY"
    HW_NOT_READY        = "HW_NOT_READY"
    PAGE_READ_OK        = "PAGE_READ_OK"
    PAGE_READ_FAIL      = "PAGE_READ_FAIL"
    PAGE_SKIPPED        = "PAGE_SKIPPED"          # salto detectado

    # Confirmação de sumário
    CONFIRM_READ        = "CONFIRM_READ"
    CONFIRM_WAITING     = "CONFIRM_WAITING"
    CONFIRM_TIMEOUT     = "CONFIRM_TIMEOUT"

    # Retry de CRC
    PAGE_CRC_RETRY          = "PAGE_CRC_RETRY"       # tentativa de re-leitura
    PAGE_PERMANENTLY_LOST   = "PAGE_PERMANENTLY_LOST" # falhou todas as tentativas

    # Clear database
    CLEAR_DB_TRIGGERED  = "CLEAR_DB_TRIGGERED"
    CLEAR_DB_POLLING    = "CLEAR_DB_POLLING"
    CLEAR_DB_DONE       = "CLEAR_DB_DONE"

    # Estado interno
    STATE_SNAPSHOT      = "STATE_SNAPSHOT"


# ---------------------------------------------------------------------------
# Logger principal
# ---------------------------------------------------------------------------

class ExperimentDebugLogger:
    """
    Grava eventos estruturados em arquivo .jsonl rotativo.

    Uso:
        dbg = ExperimentDebugLogger(log_dir="logs/experiment_debug")
        dbg.cycle_start(cycle_no, state_snapshot)
        dbg.hw_status(status_dict)
        dbg.summary_loaded(offset, entries)
        dbg.page_read_ok(flash_page, results_count, prev_page)
        ...
    """

    _MEGABYTE = 1_048_576

    def __init__(
        self,
        log_dir: str = "logs",
        filename: str = "experiment_debug.jsonl",
        max_bytes: int = 10 * _MEGABYTE,
        backup_count: int = 5,
    ) -> None:
        self._lock = threading.Lock()
        self._cycle: int = 0
        self._last_page_read: int = -1  # para detectar saltos

        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        full_path = path / filename

        handler = RotatingFileHandler(
            full_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))

        self._logger = logging.getLogger(f"exp_debug_{id(self)}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(handler)
        self._logger.propagate = False

        self._write(EventTag.STATE_SNAPSHOT, {
            "msg": "Logger iniciado",
            "log_file": str(full_path),
        })

    # ------------------------------------------------------------------
    # Métodos de evento — chamados pelo HardwareExperimentService
    # ------------------------------------------------------------------

    def cycle_start(self, cycle_no: int, state: Dict[str, Any]) -> None:
        self._cycle = cycle_no
        self._write(EventTag.CYCLE_START, {"cycle": cycle_no, "state": state})

    def cycle_end(self) -> None:
        self._write(EventTag.CYCLE_END, {})

    # --- Hardware status ---

    def hw_status_ok(self, status: Dict[str, Any]) -> None:
        self._write(EventTag.HW_STATUS_OK, {
            "unread":   status.get("qtd_experiments_unread"),
            "finished": status.get("qtd_experiments_finished"),
            "total":    status.get("qtd_experiments_total"),
            "running":  status.get("qtd_experiments_running"),
            "pending":  status.get("qtd_scheduler_pending"),
        })

    def hw_status_fail(self) -> None:
        self._write(EventTag.HW_STATUS_FAIL, {})

    # --- Boundary de experimento (flash_page reinicia em 0) ---

    def experiment_boundary(self, last_page: int) -> None:
        """
        Chamado quando um experimento encerra e o próximo começará em flash_page=0.
        Reseta last_page_read para evitar falso PAGE_SKIPPED no início da nova rodada.
        """
        self._last_page_read = -1
        self._write(EventTag.STATE_SNAPSHOT, {
            "msg":       "experiment_boundary — last_page_read resetado",
            "last_page": last_page,
        })

    def hw_reset_detected(self, last_total: int) -> None:
        # Reseta last_page_read para não gerar PAGE_SKIPPED falso quando
        # o hardware inicia um novo experimento e flash_page volta a 0.
        self._last_page_read = -1
        self._write(EventTag.HW_RESET_DETECTED, {"last_total": last_total})

    # --- Sumário ---

    def summary_loaded(
        self,
        offset: int,
        summary_page: int,
        entries: List[Dict[str, Any]],
    ) -> None:
        not_read = [i for i, e in enumerate(entries) if e.get("status") == "NOT_READ"]
        read     = [i for i, e in enumerate(entries) if e.get("status") == "READ"]
        empty    = [i for i, e in enumerate(entries) if e.get("status") not in ("NOT_READ", "READ")]

        self._write(EventTag.SUMMARY_LOADED, {
            "offset":       offset,
            "summary_page": summary_page,
            "total":        len(entries),
            "not_read_idx": not_read,
            "read_idx":     read,
            "other_idx":    empty,
        })
        # Dump completo das entries para reconstrução offline
        self._write(EventTag.SUMMARY_ENTRIES, {
            "offset":  offset,
            "entries": [
                {"i": i, "id": e.get("id"), "status": e.get("status")}
                for i, e in enumerate(entries)
            ],
        })

    def summary_load_fail(self, offset: int, reason: str) -> None:
        self._write(EventTag.SUMMARY_LOAD_FAIL, {"offset": offset, "reason": reason})

    # --- Varredura ---

    def offset_scan(self, offset: int, offsets_scanned: int, max_offsets: int) -> None:
        self._write(EventTag.OFFSET_SCAN, {
            "offset":          offset,
            "offsets_scanned": offsets_scanned,
            "max_offsets":     max_offsets,
        })

    def offset_no_not_read(self, offset: int, next_offset: int, offsets_scanned: int) -> None:
        self._write(EventTag.OFFSET_NO_NOT_READ, {
            "offset":          offset,
            "next_offset":     next_offset,
            "offsets_scanned": offsets_scanned,
        })

    def offset_found(
        self,
        offset: int,
        entry_idx: int,
        entry_id: int,
        flash_page: int,
    ) -> None:
        self._write(EventTag.OFFSET_FOUND, {
            "offset":     offset,
            "entry_idx":  entry_idx,
            "entry_id":   entry_id,
            "flash_page": flash_page,
        })

    # --- Requisição de página ---

    def request_sent(self, flash_page: int, exp_id: int) -> None:
        self._write(EventTag.REQUEST_SENT, {
            "flash_page": flash_page,
            "exp_id":     exp_id,
        })

    def request_rejected(self, flash_page: int, exp_id: int, response: Any) -> None:
        self._write(EventTag.REQUEST_REJECTED, {
            "flash_page": flash_page,
            "exp_id":     exp_id,
            "response":   str(response),
        })

    def request_timeout(self, flash_page: int, elapsed: float) -> None:
        self._write(EventTag.REQUEST_TIMEOUT, {
            "flash_page": flash_page,
            "elapsed_s":  round(elapsed, 2),
        })

    # --- Leitura de buffer ---

    def hw_ready(self, flash_page: int) -> None:
        self._write(EventTag.HW_READY, {"flash_page": flash_page})

    def hw_not_ready(self, flash_page: int, elapsed: float) -> None:
        self._write(EventTag.HW_NOT_READY, {
            "flash_page": flash_page,
            "elapsed_s":  round(elapsed, 2),
        })

    def page_read_ok(
        self,
        flash_page: int,
        results_count: int,
        next_page: int,
    ) -> None:
        # Detecta salto de página
        if self._last_page_read >= 0 and flash_page != self._last_page_read + 1:
            self._write(EventTag.PAGE_SKIPPED, {
                "expected_page": self._last_page_read + 1,
                "got_page":      flash_page,
                "gap":           flash_page - (self._last_page_read + 1),
            })

        self._last_page_read = flash_page
        self._write(EventTag.PAGE_READ_OK, {
            "flash_page":    flash_page,
            "results_count": results_count,
            "next_page":     next_page,
        })

    def page_read_fail(self, flash_page: int, reason: str) -> None:
        self._write(EventTag.PAGE_READ_FAIL, {
            "flash_page": flash_page,
            "reason":     reason,
        })

    def page_crc_retry(self, flash_page: int, attempt: int, max_retries: int) -> None:
        self._write(EventTag.PAGE_CRC_RETRY, {
            "flash_page":  flash_page,
            "attempt":     attempt,
            "max_retries": max_retries,
        })

    def page_permanently_lost(self, flash_page: int, total_attempts: int) -> None:
        # Avança last_page_read para não gerar PAGE_SKIPPED falso na próxima leitura
        self._last_page_read = flash_page
        self._write(EventTag.PAGE_PERMANENTLY_LOST, {
            "flash_page":     flash_page,
            "total_attempts": total_attempts,
        })

    # --- Confirmação ---

    def confirm_read(self, page: int) -> None:
        self._write(EventTag.CONFIRM_READ, {"page": page})

    def confirm_waiting(self, page: int, entry_status: str, elapsed: float) -> None:
        self._write(EventTag.CONFIRM_WAITING, {
            "page":         page,
            "entry_status": entry_status,
            "elapsed_s":    round(elapsed, 2),
        })

    def confirm_timeout(self, page: int, elapsed: float) -> None:
        self._write(EventTag.CONFIRM_TIMEOUT, {
            "page":      page,
            "elapsed_s": round(elapsed, 2),
        })

    # --- Clear database ---

    def clear_db_triggered(self, reason: str, flash_page: int) -> None:
        self._write(EventTag.CLEAR_DB_TRIGGERED, {
            "reason":     reason,
            "flash_page": flash_page,
        })

    def clear_db_polling(self, elapsed: float, unread: int) -> None:
        self._write(EventTag.CLEAR_DB_POLLING, {
            "elapsed_s": round(elapsed, 2),
            "unread":    unread,
        })

    def clear_db_done(self) -> None:
        self._write(EventTag.CLEAR_DB_DONE, {})

    # --- Snapshot de estado ---

    def state_snapshot(self, state: Dict[str, Any]) -> None:
        self._write(EventTag.STATE_SNAPSHOT, state)

    # ------------------------------------------------------------------
    # Núcleo de escrita
    # ------------------------------------------------------------------

    def _write(self, tag: EventTag, payload: Dict[str, Any]) -> None:
        record = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "cycle":   self._cycle,
            "event":   tag.value,
            "payload": payload,
        }
        with self._lock:
            self._logger.debug(json.dumps(record, ensure_ascii=False))