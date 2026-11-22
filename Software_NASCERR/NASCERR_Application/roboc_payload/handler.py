from __future__ import annotations

import csv
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set

from nascerr_modules.nascerr_operation import Operation
from nascerr_modules.nascerr_system import NASCERR_ACK

from app.models import (
    ExperimentCommand,
    ExperimentResultBatch,
    LogPage,
    TelemetryFrame,
)
from payloads.base import PayloadHandler
from roboc_payload.firmware_update import FirmwareUpdater
from roboc_payload.experiments import HardwareExperimentService
from roboc_payload.gnss import GnssReader
from roboc_payload.hw_manager import HardwareManager
from roboc_payload.logs import LogSync
from roboc_payload.image_report import ImageReportReader
from nascerr_logging.mylogger import get_logger

system_logger = get_logger("roboc")


class RoboCHandler(PayloadHandler):
    """
    Implementação do PayloadHandler para o payload RoboC.
    Compõe HardwareManager, HardwareExperimentService, GnssReader e LogSync.
    O NascerrClient nunca precisa conhecer nenhum desses módulos diretamente.
    """

    def __init__(self, device_name: str, i2c_address: int = 0x55) -> None:
        self._device_name = device_name
        self._hw_lock = threading.Lock()

        self._op = Operation(device_name, i2c_address)

        self._hw = HardwareManager(self._op, self._hw_lock)
        self._exp = HardwareExperimentService(self._op, self._hw_lock)
        self._gnss = GnssReader()
        self._logs = LogSync(self._op, self._hw_lock)
        self._update = FirmwareUpdater(self._op, self._hw_lock)
        self._image_reporter = ImageReportReader(self._op, self._hw_lock)

        # CMDs despachados cujos resultados ainda não foram coletados
        self._dispatched_pending: Set[int] = set()

        # CSV de sensores
        log_dir = Path("dados_salvos")
        log_dir.mkdir(exist_ok=True)
        run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._csv_path = log_dir / f"hardware_sensors_{run_id}.csv"
        self._csv_lock = threading.Lock()
        self._init_csv()

    # ------------------------------------------------------------------
    # PayloadHandler — interface pública
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Acorda o hardware e sincroniza o estado inicial dos experimentos."""

        retry_interval = 5  # segundos
        attempt = 0
        while True:
            attempt += 1
            try:
                self._hw.wakeup()
                break
            except ConnectionError as e:
                system_logger.warning(
                    f"[Tentativa {attempt}] Placa desconectada. {e}."
                    f"Tentando novamente em {retry_interval} segundos..."
                )
            time.sleep(retry_interval)
        
        self._hw.start_reconnect_watchdog() # watchdog roda em background a partir daqui
        self._exp.sync_initial_state()
        system_logger.info("Inicialização do roboc concluída.")

    def read_telemetry(self) -> TelemetryFrame:
        """Lê sensores + GNSS e persiste no CSV."""
        sensors = self._hw.read_sensors()
        gnss = self._gnss.read()
        image_report = self._image_reporter.read()

        self._save_csv(sensors)

        return TelemetryFrame(
            device_name=self._device_name,
            hw=sensors,
            gnss=gnss,
            image=image_report,
            ts_unix=int(time.time()),
        )

    def execute_command(self, cmd: ExperimentCommand) -> None:
        """
        Despacha o comando para o hardware com retries locais (Phase 1 e 2).
        Lança RuntimeError em falha após 3 tentativas.
        """
        max_retries = 3
        last_err = None

        for attempt in range(1, max_retries + 1):
            try:
                # Phase 1: Dispatch (O seu _exp.dispatch deve lidar com o envio I2C)
                system_logger.info(f"[HW] Tentativa {attempt}: Despachando CMD {cmd.id}")
                self._exp.dispatch(cmd)
                
                # Phase 2: Aguardar conclusão (Polling de status no hardware)
                # Implementamos um helper para verificar se o hardware terminou com sucesso
                self._poll_hardware_completion(cmd.id)
                
                system_logger.info(f"[HW] CMD {cmd} executado com sucesso na tentativa {attempt}.")
                self._dispatched_pending.add(cmd.id)
                return # SUCESSO ABSOLUTO: sai do método e o cliente gRPC enviará o ACK_SUCCESS

            except Exception as e:
                last_err = e
                system_logger.warning(
                    f"[HW] Falha na tentativa {attempt} para CMD {cmd.id}: {e}. "
                    f"Re-tentando em 10s..."
                )
                time.sleep(10) # Delay entre tentativas de hardware

        # Se o loop terminar sem o 'return', significa que todas as tentativas falharam
        raise RuntimeError(f"Erro crítico de Hardware após {max_retries} tentativas: {last_err}")

    def _poll_hardware_completion(self, cmd_id: int, timeout: int = 10) -> None:
        """
        Realiza o polling no HardwareExperimentService até obter sucesso ou falha.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:

            ack_status = self._exp._get_ack_stage()
            ack_status = (ack_status["data"]["ACK info"]["value"])

            if ack_status == NASCERR_ACK.ACK_SUCESS:
                return
            if ack_status == NASCERR_ACK.ACK_FAIL:
                raise RuntimeError("O Hardware reportou falha na execução (ACK_FAIL).")
            
            # Se estiver em "CMD_PROCESSING" ou similar, apenas aguarda
            time.sleep(1)
            
        raise TimeoutError(f"Timeout de {timeout}s aguardando conclusão do CMD {cmd_id}.")

    def collect_results(self) -> Optional[ExperimentResultBatch]:
        """
        Chama o leitor de experimentos (máquina de estados).
        Retorna None enquanto não há resultado pronto.
        """
        batch = self._exp.read_next()
        if batch is not None:
            batch.device_name = self._device_name
            for r in batch.results:
                self._dispatched_pending.discard(r.experiment_id)
        return batch

    def collect_watchdog_failures(self) -> Optional[List[int]]:
        """
        Retorna None se nenhum reset ocorreu.
        Retorna [] se houve reset mas não havia CMDs pendentes.
        Retorna [ids] se houve reset com CMDs perdidos.
        """
        if not self._exp.was_watchdog_reset():
            return None
        failed = list(self._dispatched_pending)
        self._dispatched_pending.clear()
        if failed:
            system_logger.warning(
                f"[WATCHDOG] {len(failed)} CMD(s) perdido(s) por reset: {failed}"
            )
        else:
            system_logger.warning("[WATCHDOG] Reset detectado sem CMDs pendentes.")
        return failed

    def sync_logs(self) -> List[LogPage]:
        """Coleta páginas de log NOT_READ do hardware."""
        return self._logs.collect()

    def apply_firmware_update(self, firmware_data: bytes, firmware_version: int) -> None:
        """Aplica atualização de firmware. Lança RuntimeError em falha."""
        self._update.execute_update(new_version=firmware_version, firmware_data=firmware_data, packet_size=256)

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _init_csv(self) -> None:
        with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "timestamp_utc", "temperature_c", "power_total",
                "current_1V2", "current_2V5", "current_2V5vpp",
                "current_3V3", "current_3V3_FPGA",
            ])

    def _save_csv(self, sensors) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        c = sensors.currents
        row = [
            ts,
            sensors.temperature_c,
            sensors.total_power,
            c.get("1V2", 0.0),
            c.get("2V5", 0.0),
            c.get("2V5vpp", 0.0),
            c.get("3V3", 0.0),
            c.get("3V3_FPGA", 0.0),
        ]
        with self._csv_lock:
            with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)

    def read_firmware_version(self) -> str:
        return self._hw.read_firmware_version()
