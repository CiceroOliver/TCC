"""
Campanha funcional da carga útil NASCERR (ensaio em solo, sem radiação).

Os cenários seguem a tabela "Categorias e cenários da campanha funcional":

    Hardware ............ leitura de corrente e temperatura
                          ciclo de sleep e wakeup
    Sistema de log ...... preenchimento da flash interna
                          migração para a flash externa
    Bootloader .......... atualização completa de firmware
    Experimento ......... agendamento de escritas
                          leitura com ECC
    Operação geral ...... taxas crescentes de comandos
                          saturação da fila
                          carga contínua (soak)

O script usa a mesma base do nascerr_tool.py, a classe Operation de
NASCERR_Application/nascerr_modules, e roda no computador de bordo (mestre
I2C) ligado à carga útil. Cada teste grava suas medidas em
resultados/<data_hora>/.

Execução:
    pytest -v test_campanha_funcional.py                 campanha completa
    pytest -v -m "not soak" test_campanha_funcional.py   sem o ensaio de 30 min
    pytest -v -m hardware test_campanha_funcional.py     uma categoria

Variáveis de ambiente:
    NASCERR_APP_DIR        pasta que contém nascerr_modules
                           (padrão ../NASCERR_Application)
    NASCERR_I2C_ADDR       endereço do escravo (padrão 0x55)
    NASCERR_FW_BIN         imagem .bin usada no ensaio do bootloader
    NASCERR_FW_VERSION     versão gravada na atualização (padrão: atual + 1)
    CAMPANHA_SOAK_MIN      duração total do soak em minutos (padrão 30)
    CAMPANHA_FASE1_MIN     duração da primeira fase do soak (padrão 10)
    CAMPANHA_RESULTADOS    pasta de saída (padrão ./resultados)

Por projeto, o comando SLEEP descarta as áreas de escalonador e de experimentos
e o ensaio do bootloader reinicia a carga útil. Por isso a ordem dos testes
segue a tabela e cada categoria parte de um estado limpo.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, os.getenv(
    "NASCERR_APP_DIR",
    str(Path(__file__).resolve().parents[1] / "NASCERR_Application")))

from nascerr_modules.nascerr_command import CmdTypeDef, W_DataTypeDef  # noqa: E402
from nascerr_modules.nascerr_operation import Operation                 # noqa: E402
from nascerr_modules.nascerr_system import (                            # noqa: E402
    FlashRegionTypeDef, NASCERR_MODE,
)


# =============================================================================
# Parâmetros da campanha
# =============================================================================

# Faixas de referência medidas previamente na bancada, com a carga útil
# ociosa e acordada. Ajustar antes da campanha.
FAIXA_TEMPERATURA_C = (15.0, 45.0)
FAIXA_CORRENTE_TRILHO_MA = (0.0, 500.0)
FAIXA_CORRENTE_TOTAL_MA = (1.0, 1500.0)
AMOSTRAS_HW = 10

# Constantes do firmware
TOTAL_BLOCOS_SRAM = 64          # blocos da memória robusta
CAPACIDADE_FILA = 64            # CMD_QUEUE_LENGTH = MAX_SCHEDULER
LOGS_POR_PAGINA = 301           # LOG_BUFFER_MAX_SIZE
ENTRADAS_POR_SUMARIO = 30       # EXP_UNREAD_PAGE_SIZE
OFFSET_MAX_SUMARIO = 210
NIVEIS_FILA = ["below_50_percent", "between_50_and_75_percent",
               "between_75_and_100_percent", "100_percent"]

LOG_STATUS_VALIDOS = {1, 2, 3}  # LOG_STATUS_SUCCESS, WARN e ERROR

# Sistema de log
MAX_CMDS_POR_MIGRACAO = 400     # cada escrita gera pelo menos 2 registros

# Experimento
AGENDAMENTO_BASE_MS = 2000
AGENDAMENTO_PASSO_MS = 250
TIMEOUT_EXECUCAO_64_S = 600

# Operação geral
INTERVALOS_MS = [1000, 500, 250, 100, 50]
CMDS_POR_TAXA = 20
AGENDAMENTO_LONGE_MS = 3_600_000   # 1 h, mantém as escritas paradas no escalonador
TIMEOUT_DRENAGEM_S = 60

SOAK_TOTAL_S = float(os.getenv("CAMPANHA_SOAK_MIN", "30")) * 60
SOAK_FASE1_S = float(os.getenv("CAMPANHA_FASE1_MIN", "10")) * 60
SOAK_LOTE_FASE1 = 20
SOAK_LOTE_MIN, SOAK_LOTE_MAX = 15, 30
SOAK_INTERVALO_MIN_S, SOAK_INTERVALO_MAX_S = 10, 25
SOAK_MAX_BLOCOS_CMD = 4
SOAK_TIMEOUT_DRENAGEM_S = 300
SOAK_MAX_FALHAS_SEGUIDAS = 5


# =============================================================================
# Infraestrutura
# =============================================================================

class ErroNascerr(ValueError):
    """Resposta de Operation marcada com erro."""


def dados(resp: dict):
    """Devolve resp['data'] ou levanta ErroNascerr com a causa do erro."""
    if not resp or resp.get("error"):
        raise ErroNascerr((resp or {}).get("log_error")
                          or (resp or {}).get("message") or "sem resposta")
    return resp.get("data")


def aguardar(condicao: Callable[[], bool], timeout_s: float,
             periodo_s: float = 0.1) -> bool:
    """Avalia condicao() até ser verdadeira ou o tempo esgotar. Erros de
    barramento e de CRC contam como condição falsa."""
    limite = time.monotonic() + timeout_s
    while time.monotonic() < limite:
        try:
            if condicao():
                return True
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(periodo_s)
    return False


def percentil(valores: List[float], p: float) -> float:
    if not valores:
        return math.nan
    v = sorted(valores)
    k = (len(v) - 1) * p / 100
    i = int(k)
    return v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * (k - i)


class Relatorio:
    """Guarda as medidas de cada cenário em JSON e CSV."""

    def __init__(self, pasta: Path):
        self.pasta = pasta
        self.pasta.mkdir(parents=True, exist_ok=True)

    def json(self, nome: str, conteudo) -> None:
        with open(self.pasta / f"{nome}.json", "w", encoding="utf-8") as f:
            json.dump(conteudo, f, indent=2, ensure_ascii=False, default=str)

    def csv(self, nome: str, linhas: List[Dict]) -> None:
        if not linhas:
            return
        with open(self.pasta / f"{nome}.csv", "w", newline="",
                  encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()))
            w.writeheader()
            w.writerows(linhas)


@pytest.fixture(scope="session")
def op():
    o = Operation("NASCERR_CargaUtil", int(os.getenv("NASCERR_I2C_ADDR", "0x55"), 0))
    assert aguardar(lambda: dados(o.get_status()) is not None, 10), \
        "carga útil não responde no barramento I2C"
    return o


@pytest.fixture(scope="session")
def relatorio():
    base = Path(os.getenv("CAMPANHA_RESULTADOS", "resultados"))
    return Relatorio(base / time.strftime("%Y%m%d_%H%M%S"))


# ------------------------------------------------- leitura de telemetria
def status(op: Operation) -> dict:
    return dados(op.get_status())


def estado_hw(op: Operation) -> str:
    return status(op)["operation"]["HW"]["name"]


def status_exp(op: Operation) -> dict:
    return dados(op.get_experiment_status())


def nivel_fila(st: dict) -> int:
    return NIVEIS_FILA.index(st["fail_flag"]["queue_level_description"])


def fase_ack(op: Operation) -> str:
    return dados(op.get_ack())["ACK info"]["state"]


# ------------------------------------------------------- telecomandos
def escrita(op: Operation, id_: int, bloco: int, qtd: int = 1,
            ecc: NASCERR_MODE = NASCERR_MODE.MRSC,
            padrao: W_DataTypeDef = W_DataTypeDef.WRITE_xAAh,
            agendamento_ms: int = 1000, delay_ms: int = 1000) -> dict:
    return op.send_commandNew(
        cmd_type=CmdTypeDef.WRITE_CMD, id=id_, block=bloco, qtd_block=qtd,
        ecc=ecc, write_type=padrao, scheduler_time=agendamento_ms,
        repeat=0, delay=delay_ms, current_tick=0)


def escrita_sem_espera(op: Operation, id_: int, bloco: int,
                       agendamento_ms: int) -> dict:
    """Escrita direto na camada Protocol, sem a pausa de 50 ms de Operation.
    Usada nas rajadas, em que o intervalo é controlado pelo próprio ensaio."""
    return op.communication.set_commandNew(
        CmdTypeDef.WRITE_CMD, id_, bloco, 1, NASCERR_MODE.MRSC,
        W_DataTypeDef.WRITE_xAAh, agendamento_ms, 0, 0, 0)


def escrita_confirmada(op: Operation, *args, timeout_s: float = 3.0,
                       **kwargs) -> bool:
    """
    Envia uma escrita e espera o veredito do escalonador. O registrador ACK é
    único e pode guardar a fase do comando anterior, então a escrita só é
    considerada aceita quando o ACK chega a ACK_SUCESS depois de o comando
    ter sido visto em andamento, ou quando qtd_scheduler_total muda.
    """
    total0 = status_exp(op)["qtd_scheduler_total"]
    dados(escrita(op, *args, **kwargs))
    em_andamento = False
    limite = time.monotonic() + timeout_s
    while time.monotonic() < limite:
        try:
            fase = fase_ack(op)
            mudou = status_exp(op)["qtd_scheduler_total"] != total0
        except ValueError:
            continue
        if fase in ("ACK_WRITE_RECEIVED", "ACK_CMD_PROCESSING"):
            em_andamento = True
        if fase == "ACK_SUCESS" and (em_andamento or mudou):
            return True
        if fase == "ACK_FAIL" and (em_andamento or not mudou):
            return False
        time.sleep(0.01)
    return False


def limpar_experimentos(op: Operation) -> None:
    """Parte de um estado limpo: escalonador e experimentos vazios."""
    dados(op.clear_database(FlashRegionTypeDef.FLASH_SCHEDULER))
    time.sleep(0.3)
    dados(op.clear_database(FlashRegionTypeDef.FLASH_EXPERIMENT))
    assert aguardar(lambda: status_exp(op)["qtd_scheduler_total"] == 0
                    and status_exp(op)["qtd_experiments_unread"] == 0, 10), \
        "limpeza das regiões de escalonador e experimentos não concluiu"


def novo_id() -> int:
    """Identificador único de experimento, derivado do relógio."""
    novo_id.ultimo = max(getattr(novo_id, "ultimo", 0) + 1,
                         int(time.time() * 1000) & 0xFFFFFF)
    return novo_id.ultimo


class LeitorExperimentos:
    """
    Fluxo de leitura do OBC, o mesmo de roboc_payload.HardwareExperimentService
    em forma síncrona: procura uma página NOT_READ no sumário, envia READ_CMD,
    espera REQ_EXP_TX_RESULT no STAT, lê IDX+TXB e aguarda o sumário marcar a
    página como lida.
    """

    def __init__(self, op: Operation, timeout_pronto_s: float = 15.0,
                 timeout_confirmacao_s: float = 10.0):
        self.op = op
        self.timeout_pronto_s = timeout_pronto_s
        self.timeout_confirmacao_s = timeout_confirmacao_s
        self.falhas_leitura = 0

    def _sumario(self, offset: int) -> List[dict]:
        dados(self.op.set_exp_sum_idx(offset))
        time.sleep(0.1)
        return dados(self.op.get_exp_sum())["entries"]

    def _proxima_nao_lida(self) -> Optional[Tuple[int, dict]]:
        for offset in range(0, OFFSET_MAX_SUMARIO + 1, ENTRADAS_POR_SUMARIO):
            entradas = self._sumario(offset)
            for i, e in enumerate(entradas):
                if e["status"] == "NOT_READ":
                    return offset + i, e
            if not entradas:
                break
        return None

    def ler_proxima(self) -> Optional[List[dict]]:
        """Lê uma página não lida. Devolve None se não houver nenhuma."""
        alvo = self._proxima_nao_lida()
        if alvo is None:
            return None
        pagina, entrada = alvo
        dados(self.op.send_commandNew(
            cmd_type=CmdTypeDef.READ_CMD, id=int(entrada["id"]), block=pagina,
            qtd_block=0, ecc=NASCERR_MODE.MRSC,
            write_type=W_DataTypeDef.WRITE_ZEROS, scheduler_time=0,
            repeat=0, delay=0, current_tick=0))

        def req_exp():
            return status(self.op)["request"]["EXP"]["name"]

        aguardar(lambda: req_exp() in ("REQ_EXP_TX_RESULT", "REQ_EXP_FAIL"),
                 self.timeout_pronto_s, 0.05)
        if req_exp() != "REQ_EXP_TX_RESULT":
            self.falhas_leitura += 1
            return []
        resultados = None
        for _ in range(3):
            r = self.op.read_experiment()
            if not r.get("error"):
                resultados = r["data"]
                break
            self.falhas_leitura += 1
            time.sleep(0.05)
        if resultados is None:
            return []

        def confirmada() -> bool:
            if status_exp(self.op)["qtd_experiments_unread"] == 0:
                return True
            ent = self._sumario(pagina - pagina % ENTRADAS_POR_SUMARIO)
            i = pagina % ENTRADAS_POR_SUMARIO
            return i >= len(ent) or ent[i]["status"] != "NOT_READ"

        aguardar(confirmada, self.timeout_confirmacao_s, 0.1)
        return resultados

    def ler_todas(self, timeout_s: float) -> List[dict]:
        """Lê páginas até qtd_experiments_unread zerar ou o tempo esgotar."""
        lidos: List[dict] = []
        limite = time.monotonic() + timeout_s
        while time.monotonic() < limite:
            if status_exp(self.op)["qtd_experiments_unread"] == 0:
                break
            lote = self.ler_proxima()
            if lote:
                lidos.extend(lote)
            elif lote is None:
                time.sleep(0.5)
        return lidos


# =============================================================================
# 1. Hardware
# =============================================================================

@pytest.mark.hardware
def test_hw_leitura_corrente_temperatura(op, relatorio):
    """As medidas reportadas ficam dentro das faixas de referência."""
    amostras = []
    for _ in range(AMOSTRAS_HW):
        t = dados(op.get_hardware_temperature())[0]
        i = dados(op.get_hardware_current())
        amostras.append({"temperatura_c": t,
                         **{f"i{k}_ma": v for k, v in enumerate(i)},
                         "i_total_ma": sum(i)})
        time.sleep(0.2)
    relatorio.csv("hw_leituras", amostras)

    for a in amostras:
        assert all(math.isfinite(v) for v in a.values())
        assert FAIXA_TEMPERATURA_C[0] <= a["temperatura_c"] <= FAIXA_TEMPERATURA_C[1]
        for k in range(5):
            assert FAIXA_CORRENTE_TRILHO_MA[0] <= a[f"i{k}_ma"] \
                <= FAIXA_CORRENTE_TRILHO_MA[1], f"trilho {k} fora da faixa"
        assert FAIXA_CORRENTE_TOTAL_MA[0] <= a["i_total_ma"] \
            <= FAIXA_CORRENTE_TOTAL_MA[1]

    temps = [a["temperatura_c"] for a in amostras]
    assert statistics.pstdev(temps) < 2.0, "temperatura instável em repouso"


@pytest.mark.hardware
def test_hw_ciclo_sleep_wakeup(op, relatorio):
    """O sistema entra em baixo consumo e retorna à operação nominal."""
    assert estado_hw(op) == "OP_HW_OK"

    t0 = time.monotonic()
    assert not op.system_sleep().get("error")
    dormiu = aguardar(lambda: estado_hw(op) == "OP_HW_SLEEP", 5)
    t_sleep = time.monotonic() - t0
    time.sleep(2.0)

    t0 = time.monotonic()
    op.system_wakeup()
    acordou = aguardar(lambda: estado_hw(op) == "OP_HW_OK", 5)
    t_wakeup = time.monotonic() - t0

    # Operação nominal: telemetria e escalonador voltam a responder
    time.sleep(1.0)
    t = dados(op.get_hardware_temperature())[0]
    limpar_experimentos(op)
    escalonador_ok = escrita_confirmada(op, novo_id(), 0,
                                        agendamento_ms=AGENDAMENTO_LONGE_MS)
    limpar_experimentos(op)

    relatorio.json("hw_sleep_wakeup", {
        "dormiu": dormiu, "tempo_ate_sleep_s": t_sleep,
        "acordou": acordou, "tempo_ate_wakeup_s": t_wakeup,
        "temperatura_pos_wakeup_c": t, "escalonador_pos_wakeup": escalonador_ok})

    assert dormiu, "STAT não indicou OP_HW_SLEEP após o comando SLEEP"
    assert acordou, "STAT não voltou a OP_HW_OK após o WAKEUP"
    assert FAIXA_TEMPERATURA_C[0] <= t <= FAIXA_TEMPERATURA_C[1]
    assert escalonador_ok, "o escalonador não aceitou escrita após o wakeup"


# =============================================================================
# 2. Sistema de log
# =============================================================================

def total_paginas_log(op: Operation) -> int:
    return dados(op.get_log_summary())["total_pages"]


def gerar_logs_ate_migrar(op: Operation, paginas_iniciais: int) -> Dict:
    """
    Gera registros até o firmware migrar a flash interna para uma nova página
    externa. Cada escrita no bloco 0, já ocupado pela âncora, é recusada por
    conflito e produz LOG_RECEIVE_COMMAND e ERR_EXP_CONFLICT sem consumir o
    escalonador.
    """
    enviados, falhas_stat = 0, 0
    while enviados < MAX_CMDS_POR_MIGRACAO:
        escrita(op, novo_id(), 0, agendamento_ms=AGENDAMENTO_LONGE_MS)
        enviados += 1
        time.sleep(0.01)
        if op.get_status().get("error"):     # o escalonador continua respondendo?
            falhas_stat += 1
        if enviados % 5 == 0 and aguardar(
                lambda: total_paginas_log(op) > paginas_iniciais, 0.2):
            break
    migrou = aguardar(lambda: total_paginas_log(op) > paginas_iniciais, 5)
    return {"comandos": enviados, "migrou": migrou, "falhas_stat": falhas_stat}


def ler_pagina_log(op: Operation, pagina: int) -> List[dict]:
    dados(op.set_log_page(pagina))
    assert aguardar(lambda: dados(op.get_log_summary())["selected_page"]
                    == pagina, 5), f"página de log {pagina} não preparada"
    time.sleep(0.1)
    return dados(op.get_log_page_content())


def validar_pagina_log(registros: List[dict]) -> List[str]:
    """
    Problemas encontrados em uma página migrada. O critério é só a chegada
    íntegra na flash externa: página completa, status válido e timestamps em
    ordem. O conteúdo de tipo e código de erro não é avaliado.
    """
    problemas = []
    if len(registros) != LOGS_POR_PAGINA:
        problemas.append(f"{len(registros)} registros (esperado {LOGS_POR_PAGINA})")
    for k, r in enumerate(registros):
        if r["status"] not in LOG_STATUS_VALIDOS:
            problemas.append(f"registro {k}: status {r['status']}")
        if k and r["timestamp"] < registros[k - 1]["timestamp"]:
            problemas.append(f"registro {k}: timestamp regrediu")
    return problemas


@pytest.fixture(scope="module")
def estado_log(op):
    """Âncora no bloco 0 e área de logs zerada, compartilhadas pelos 2 cenários."""
    limpar_experimentos(op)
    dados(op.clear_database(FlashRegionTypeDef.FLASH_LOGS))
    time.sleep(0.5)
    assert escrita_confirmada(op, novo_id(), 0, agendamento_ms=AGENDAMENTO_LONGE_MS)
    yield {"paginas": total_paginas_log(op)}
    limpar_experimentos(op)


@pytest.mark.log
def test_log_preenchimento_flash_interna(op, relatorio, estado_log):
    """Os registros enchem a flash interna até a primeira migração."""
    p0 = estado_log["paginas"]
    r = gerar_logs_ate_migrar(op, p0)
    relatorio.json("log_preenchimento", {"paginas_iniciais": p0, **r})

    assert r["migrou"], "a flash interna não migrou após o limite de comandos"
    assert r["falhas_stat"] == 0, "STAT deixou de responder durante a geração"
    estado_log["paginas"] = total_paginas_log(op)
    assert estado_log["paginas"] == p0 + 1

    registros = ler_pagina_log(op, p0)
    estado_log["ultima_pagina"] = registros
    assert validar_pagina_log(registros) == []


@pytest.mark.log
def test_log_migracao_flash_externa(op, relatorio, estado_log):
    """Uma nova migração ocorre sem corromper registros nem parar o escalonador."""
    if "ultima_pagina" not in estado_log:
        pytest.skip("depende do preenchimento da flash interna")
    p0 = estado_log["paginas"]
    r = gerar_logs_ate_migrar(op, p0)
    assert r["migrou"] and r["falhas_stat"] == 0

    anterior = estado_log["ultima_pagina"]
    atual = ler_pagina_log(op, p0)
    problemas = validar_pagina_log(atual)
    continuidade = bool(atual) and atual[0]["timestamp"] >= anterior[-1]["timestamp"]

    # Escalonador ativo: um experimento curto agendado agora precisa executar
    dados(op.clear_database(FlashRegionTypeDef.FLASH_SCHEDULER))
    time.sleep(0.3)
    sonda = novo_id()
    aceita = escrita_confirmada(op, sonda, 1, agendamento_ms=1000, delay_ms=500)
    executou = aguardar(lambda: status_exp(op)["qtd_experiments_unread"] >= 1, 30)
    lidos = LeitorExperimentos(op).ler_todas(30)

    relatorio.json("log_migracao", {
        "pagina": p0, **r, "problemas": problemas,
        "continuidade_timestamp": continuidade, "sonda_aceita": aceita,
        "sonda_executou": executou, "sonda_lida": lidos})

    assert problemas == []
    assert continuidade, "a página nova não continua a sequência da anterior"
    assert aceita and executou, "o escalonador parou durante a migração"
    assert any(x["id"] == sonda for x in lidos)


# =============================================================================
# 3. Bootloader
# =============================================================================

@pytest.mark.bootloader
def test_boot_atualizacao_completa(op, relatorio):
    """Atualização válida: todos os pacotes aceitos, hash conferido e saída do
    modo bootloader rodando a nova versão (Operation.update_firmware)."""
    caminho = os.getenv("NASCERR_FW_BIN")
    if not caminho or not Path(caminho).is_file():
        pytest.skip("defina NASCERR_FW_BIN com a imagem de firmware")

    anterior = dados(op.get_boot_status())
    versao = int(os.getenv("NASCERR_FW_VERSION", "0"), 0) \
        or ((anterior["version_raw"] + 1) & 0xFFFFFF)

    t0 = time.monotonic()
    bruto = op.update_firmware(caminho, new_version=versao, force=True)
    duracao = time.monotonic() - t0
    resumo = (bruto.get("summary") or {}) if bruto.get("error") else bruto
    final = dados(op.get_boot_status())
    app_ok = aguardar(lambda: dados(op.get_status()) is not None, 30)

    relatorio.json("bootloader", {
        "versao_anterior": anterior["version_raw"], "versao_pedida": versao,
        "versao_final": final["version_raw"], "flags_finais": final["flags"],
        "ultimo_erro": final["last_error"], "duracao_s": duracao,
        "retorno_update_firmware": bruto})

    assert resumo.get("total_packets"), \
        f"atualização interrompida: {bruto.get('message', bruto)}"
    assert resumo.get("successful_packets") == resumo["total_packets"], \
        "nem todos os pacotes foram confirmados"
    assert final["last_error"]["name"] != "ERR_BOOT_SHA_MISMATCH", \
        "hash SHA-256 recusado"
    assert resumo.get("left_bootloader_confirmed"), "não saiu do modo bootloader"
    assert final["version_raw"] == versao, "a nova versão não está em execução"
    assert "ERROR_DETECTED" not in final["flags"]
    assert app_ok, "a aplicação não voltou a responder após a atualização"


# =============================================================================
# 4. Experimento
# =============================================================================

@pytest.fixture(scope="module")
def agenda_64(op):
    limpar_experimentos(op)
    return {}


@pytest.mark.experimento
def test_exp_agendamento_escritas(op, relatorio, agenda_64):
    """Os 64 blocos da memória robusta são agendados e executados."""
    eccs = [NASCERR_MODE.MRSC, NASCERR_MODE.TBEC]
    padroes = [W_DataTypeDef.WRITE_ZEROS, W_DataTypeDef.WRITE_ONES,
               W_DataTypeDef.WRITE_x55h, W_DataTypeDef.WRITE_xAAh]
    linhas = []
    for bloco in range(TOTAL_BLOCOS_SRAM):
        ecc = eccs[bloco % 2]
        padrao = padroes[(bloco // 2) % 4]
        id_ = novo_id()
        aceito = escrita_confirmada(
            op, id_, bloco, 1, ecc, padrao,
            agendamento_ms=AGENDAMENTO_BASE_MS + AGENDAMENTO_PASSO_MS * bloco,
            delay_ms=1000)
        linhas.append({"id": id_, "bloco": bloco, "ecc": ecc.name,
                       "padrao": padrao.name, "aceito": aceito})
        if aceito:
            agenda_64[id_] = (bloco, ecc)
    relatorio.csv("exp_agendamento", linhas)

    assert len(agenda_64) == TOTAL_BLOCOS_SRAM, \
        f"{TOTAL_BLOCOS_SRAM - len(agenda_64)} escritas recusadas"
    st = status_exp(op)
    assert st["qtd_scheduler_total"] == TOTAL_BLOCOS_SRAM
    assert st["scheduler_full_flag"] == 1

    executou = aguardar(lambda: status_exp(op)["qtd_experiments_unread"]
                        >= TOTAL_BLOCOS_SRAM, TIMEOUT_EXECUCAO_64_S, 1.0)
    relatorio.json("exp_execucao", status_exp(op))
    assert executou, "nem todos os experimentos agendados foram executados"


@pytest.mark.experimento
def test_exp_leitura_com_ecc(op, relatorio, agenda_64):
    """Cada bloco é lido uma vez, decodificado pelo ECC pedido e sem erros."""
    if len(agenda_64) != TOTAL_BLOCOS_SRAM:
        pytest.skip("depende do agendamento dos 64 blocos")
    leitor = LeitorExperimentos(op)
    t0 = time.monotonic()
    lidos = leitor.ler_todas(timeout_s=TIMEOUT_EXECUCAO_64_S)
    relatorio.json("exp_leitura", {
        "lidos": len(lidos), "falhas_leitura": leitor.falhas_leitura,
        "tempo_s": time.monotonic() - t0, "resultados": lidos})

    por_id = {r["id"]: r for r in lidos}
    assert len(lidos) == len(por_id), "resultado duplicado"
    assert set(por_id) == set(agenda_64), "resultados faltando ou estranhos"
    for id_, (bloco, ecc) in agenda_64.items():
        r = por_id[id_]
        tel = r["ecc_telemetry"]
        assert r["block"] == bloco
        assert tel["ecc"] == ecc.name, f"bloco {bloco}: ECC {tel['ecc']}"
        assert r["data_lenght"] > 0
        assert r["number_errors"] == 0 and tel["uncorrectable_errors"] == 0, \
            f"bloco {bloco}: erros em solo"
        assert r["percent_error"] == 0.0
    assert status_exp(op)["qtd_experiments_unread"] == 0
    limpar_experimentos(op)


# =============================================================================
# 5. Operação geral
# =============================================================================

def rajada(op: Operation, n: int, intervalo_s: float) -> Dict:
    """
    Envia n escritas válidas, em blocos distintos e agendadas para longe, com
    o intervalo dado. O tempo de ciclo de cada comando vai do envio até o
    escalonador registrá-lo (qtd_scheduler_total). Como a fila é FIFO, o
    k-ésimo incremento corresponde ao k-ésimo comando.
    """
    base = status_exp(op)["qtd_scheduler_total"]
    envios: List[float] = []
    ciclos: List[float] = []
    niveis: List[int] = []
    amostras: List[Dict] = []

    def amostrar() -> bool:
        st, es = status(op), status_exp(op)
        agora = time.monotonic()
        niveis.append(nivel_fila(st))
        feitos = es["qtd_scheduler_total"] - base
        while len(ciclos) < min(feitos, len(envios)):
            ciclos.append(agora - envios[len(ciclos)])
        amostras.append({"t_s": round(agora - envios[0], 4),
                         "enviados": len(envios), "registrados": feitos,
                         "nivel_fila": NIVEIS_FILA[niveis[-1]]})
        return len(ciclos) == n

    for k in range(n):
        proximo = time.monotonic() + intervalo_s
        escrita_sem_espera(op, novo_id(), k, AGENDAMENTO_LONGE_MS)
        envios.append(time.monotonic())
        while intervalo_s and time.monotonic() < proximo:
            try:
                amostrar()
            except (OSError, ValueError):
                pass
    aguardar(amostrar, TIMEOUT_DRENAGEM_S, 0.02)
    return {"registrados": status_exp(op)["qtd_scheduler_total"] - base,
            "ciclos_s": ciclos, "nivel_max": max(niveis, default=0),
            "amostras": amostras}


@pytest.mark.operacao
def test_op_taxas_crescentes(op, relatorio):
    """Intervalos de 1000 ms a 50 ms: ocupação da fila e tempo de ciclo."""
    resumo, perdas = [], {}
    for intervalo_ms in INTERVALOS_MS:
        limpar_experimentos(op)
        r = rajada(op, CMDS_POR_TAXA, intervalo_ms / 1000)
        c_ms = [c * 1000 for c in r["ciclos_s"]]
        resumo.append({
            "intervalo_ms": intervalo_ms, "enviados": CMDS_POR_TAXA,
            "registrados": r["registrados"],
            "ciclo_mediana_ms": statistics.median(c_ms) if c_ms else math.nan,
            "ciclo_p95_ms": percentil(c_ms, 95),
            "ciclo_max_ms": max(c_ms, default=math.nan),
            "nivel_fila_max": NIVEIS_FILA[r["nivel_max"]]})
        relatorio.csv(f"op_taxa_{intervalo_ms}ms", r["amostras"])
        if r["registrados"] != CMDS_POR_TAXA:
            perdas[intervalo_ms] = CMDS_POR_TAXA - r["registrados"]
    limpar_experimentos(op)
    relatorio.csv("op_taxas_resumo", resumo)
    assert perdas == {}, f"comandos perdidos por intervalo (ms): {perdas}"


@pytest.mark.operacao
def test_op_saturacao_fila(op, relatorio):
    """Rajada de 64 comandos sem intervalo: a fila absorve tudo sem perda."""
    limpar_experimentos(op)
    t0 = time.monotonic()
    r = rajada(op, CAPACIDADE_FILA, 0)
    duracao = time.monotonic() - t0
    st = status_exp(op)
    relatorio.csv("op_saturacao", r["amostras"])
    relatorio.json("op_saturacao_resumo", {
        "enviados": CAPACIDADE_FILA, "registrados": r["registrados"],
        "nivel_fila_max": NIVEIS_FILA[r["nivel_max"]], "duracao_s": duracao,
        "scheduler_full_flag": st["scheduler_full_flag"],
        "ciclo_max_ms": max(r["ciclos_s"], default=math.nan) * 1000})
    limpar_experimentos(op)

    assert r["registrados"] == CAPACIDADE_FILA, \
        f"{CAPACIDADE_FILA - r['registrados']} comandos perdidos na rajada"
    assert st["scheduler_full_flag"] == 1


class GerenciadorBlocos:
    """Controla os 64 blocos: só agenda em blocos livres e contíguos."""

    def __init__(self):
        self.livres = set(range(TOTAL_BLOCOS_SRAM))
        self.por_id: Dict[int, range] = {}

    def alocar(self, qtd: int, id_: int) -> Optional[int]:
        ordem = sorted(self.livres)
        for i in range(len(ordem) - qtd + 1):
            if ordem[i + qtd - 1] - ordem[i] == qtd - 1:
                faixa = range(ordem[i], ordem[i] + qtd)
                self.livres -= set(faixa)
                self.por_id[id_] = faixa
                return faixa.start
        return None

    def liberar(self, id_: int) -> None:
        self.livres |= set(self.por_id.pop(id_, ()))


@pytest.mark.operacao
@pytest.mark.soak
def test_op_carga_continua_soak(op, relatorio):
    """
    Soak de 30 min sobre o ciclo de vida dos experimentos. Fase 1 (10 min):
    lote de 20 experimentos seguido de leituras. Fase 2 (20 min): lotes de 15
    a 30 experimentos a cada 10 a 25 s. A leitura deve acompanhar a produção,
    sem perda de dados nem travamento do escalonador.
    """
    limpar_experimentos(op)
    blocos = GerenciadorBlocos()
    leitor = LeitorExperimentos(op, timeout_pronto_s=10)
    esperado: Dict[int, int] = {}       # id -> resultados esperados (1 por bloco)
    recebidos: Dict[int, int] = {}
    serie: List[Dict] = []
    lotes: List[Dict] = []
    falhas = {"seguidas": 0, "pior": 0}
    t_inicio = time.monotonic()

    def agendar_lote(n: int, fase: str) -> None:
        aceitos = recusados = sem_bloco = 0
        for _ in range(n):
            qtd = random.randint(1, SOAK_MAX_BLOCOS_CMD)
            id_ = novo_id()
            bloco = blocos.alocar(qtd, id_)
            if bloco is None:
                sem_bloco += 1
                continue
            if escrita_confirmada(
                    op, id_, bloco, qtd,
                    random.choice([NASCERR_MODE.MRSC, NASCERR_MODE.TBEC]),
                    random.choice([W_DataTypeDef.WRITE_ZEROS, W_DataTypeDef.WRITE_ONES,
                                   W_DataTypeDef.WRITE_x55h, W_DataTypeDef.WRITE_xAAh]),
                    agendamento_ms=random.randint(1, 3000), delay_ms=500):
                esperado[id_] = qtd
                aceitos += 1
            else:
                blocos.liberar(id_)       # escalonador cheio ou conflito
                recusados += 1
            time.sleep(random.uniform(0.02, 0.08))
        lotes.append({"t_s": round(time.monotonic() - t_inicio, 2), "fase": fase,
                      "pedidos": n, "aceitos": aceitos, "recusados": recusados,
                      "sem_bloco_livre": sem_bloco})

    def registrar(fase: str) -> None:
        try:
            es, st = status_exp(op), status(op)
            falhas["seguidas"] = 0
        except (OSError, ValueError):
            falhas["seguidas"] += 1
            falhas["pior"] = max(falhas["pior"], falhas["seguidas"])
            assert falhas["seguidas"] < SOAK_MAX_FALHAS_SEGUIDAS, \
                "escalonador deixou de responder"
            return
        serie.append({"t_s": round(time.monotonic() - t_inicio, 2), "fase": fase,
                      "agendados": es["qtd_scheduler_pending"],
                      "executados_nao_lidos": es["qtd_experiments_unread"],
                      "finalizados": es["qtd_experiments_finished"],
                      "lidos_obc": sum(recebidos.values()),
                      "nivel_fila": st["fail_flag"]["queue_level_description"],
                      "blocos_livres": len(blocos.livres)})

    def ler_um_ciclo(fase: str) -> None:
        try:
            lote = leitor.ler_proxima()
        except ValueError:
            lote = []
        for r in lote or []:
            recebidos[r["id"]] = recebidos.get(r["id"], 0) + 1
            if recebidos[r["id"]] >= esperado.get(r["id"], 1):
                blocos.liberar(r["id"])
        registrar(fase)
        if lote is None:
            time.sleep(0.5)

    # Fase 1: lote único e leitura logo em seguida
    agendar_lote(SOAK_LOTE_FASE1, "fase1")
    while time.monotonic() - t_inicio < SOAK_FASE1_S:
        ler_um_ciclo("fase1")

    # Fase 2: lotes aleatórios a intervalos irregulares
    proxima_injecao = time.monotonic()
    while time.monotonic() - t_inicio < SOAK_TOTAL_S:
        if time.monotonic() >= proxima_injecao:
            agendar_lote(random.randint(SOAK_LOTE_MIN, SOAK_LOTE_MAX), "fase2")
            proxima_injecao = time.monotonic() + random.uniform(
                SOAK_INTERVALO_MIN_S, SOAK_INTERVALO_MAX_S)
        ler_um_ciclo("fase2")

    # Drenagem: tudo o que foi aceito precisa ser lido
    limite = time.monotonic() + SOAK_TIMEOUT_DRENAGEM_S
    while time.monotonic() < limite:
        if all(recebidos.get(i, 0) >= q for i, q in esperado.items()) and aguardar(
                lambda: status_exp(op)["qtd_experiments_unread"] == 0, 1):
            break
        ler_um_ciclo("drenagem")
    registrar("final")

    faltando = {i: q - recebidos.get(i, 0) for i, q in esperado.items()
                if recebidos.get(i, 0) < q}
    estranhos = sorted(set(recebidos) - set(esperado))
    relatorio.csv("soak_contadores", serie)
    relatorio.csv("soak_lotes", lotes)
    relatorio.json("soak_resumo", {
        "duracao_s": time.monotonic() - t_inicio,
        "experimentos_aceitos": len(esperado),
        "resultados_esperados": sum(esperado.values()),
        "resultados_lidos": sum(recebidos.values()),
        "faltando": faltando, "ids_estranhos": estranhos,
        "unread_max": max((s["executados_nao_lidos"] for s in serie), default=0),
        "falhas_leitura": leitor.falhas_leitura,
        "pior_sequencia_falhas_stat": falhas["pior"]})
    limpar_experimentos(op)

    assert esperado, "nenhum experimento foi aceito durante o soak"
    assert not faltando, f"resultados perdidos: {faltando}"
    assert not estranhos, f"resultados de ids não agendados: {estranhos}"
    assert serie[-1]["executados_nao_lidos"] == 0, \
        "a leitura não acompanhou a produção de resultados"
