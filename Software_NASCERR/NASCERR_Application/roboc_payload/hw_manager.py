from __future__ import annotations

import threading
import time
from typing import Optional

from nascerr_modules.nascerr_operation import Operation

from app.models import HardwareTelemetry

from nascerr_logging.mylogger import get_logger

RAIL_NAMES = ["1V2", "2V5", "2V5vpp", "3V3", "3V3_FPGA"]

hardware_logger = get_logger("hardware")

RECONNECT_INTERVAL = 5  # segundos entre tentativas de reconexão

class HardwareManager:
    """
    Responsável por:
    - Wakeup do hardware
    - Leitura de sensores (temperatura, potência, correntes)
    - Leitura de versão de firmware
    """

    def __init__(self, op: Operation, hw_lock: threading.Lock) -> None:
        self._op = op
        self._hw_lock = hw_lock
        self._connected = False
        self._reconnect_lock = threading.Lock()
    
    @property
    def is_connected(self) -> bool:
        return self._connected

    def wakeup(self) -> None:
        """Acorda o hardware via I2C. Lança ConnectionError se a placa não responder."""
        with self._hw_lock:
            result = self._op.system_wakeup()

        if result.get("error"):
            self._connected = False
            raise ConnectionError(
                f"Não respondeu ao wakeup. {result.get('log_error', result.get('error'))}"
            )

        time.sleep(0.5)
        self._connected = True
        hardware_logger.info("Hardware acordado.")

    def start_reconnect_watchdog(self) -> None:
        """
        Inicia thread em background que tenta reconectar quando a placa
        se desconecta. Retorna imediatamente; a thread roda até o processo encerrar.
        """
        t = threading.Thread(target=self._reconnect_loop, daemon=True, name="hw-watchdog")
        t.start()

    def _reconnect_loop(self) -> None:
        attempt = 0
        while True:
            time.sleep(1)  # verifica a cada 1s se precisa agir
            if self._connected:
                attempt = 0
                continue

            # só uma thread tenta reconectar por vez
            if not self._reconnect_lock.acquire(blocking=False):
                continue
            try:
                attempt += 1
                hardware_logger.warning(
                    f"[Watchdog] Placa desconectada. Tentativa {attempt} de reconexão..."
                )
                self.wakeup()
                hardware_logger.info("[Watchdog] Placa reconectada com sucesso.")
            except ConnectionError:
                time.sleep(RECONNECT_INTERVAL)
            finally:
                self._reconnect_lock.release()

    def read_sensors(self) -> HardwareTelemetry:
        """
        Lê temperatura, potência total e correntes por rail.
        Retorna HardwareTelemetry com status 'ERROR' se qualquer leitura falhar.
        """
        result = HardwareTelemetry()

        try:
            with self._hw_lock:
                res_temp = self._op.get_hardware_temperature()
                res_power = self._op.get_hardware_total_power()
                res_curr = self._op.get_hardware_current()

            if not res_temp.get("error") and res_temp.get("data"):
                result.temperature_c = float(res_temp["data"][0])

            if not res_power.get("error") and res_power.get("data"):
                result.total_power = float(res_power["data"][0])

            if not res_curr.get("error") and res_curr.get("data"):
                for name, val in zip(RAIL_NAMES, res_curr["data"]):
                    result.currents[name] = float(val)

        except Exception as e:
            hardware_logger.error(f"Erro ao ler sensores: {e}")
            result.status = "ERROR"
            self._connected = False # ← sinaliza para o watchdog agir

        return result

    def read_firmware_version(self) -> str:
        """Lê a versão de firmware do boot status. Retorna 'N/A' em caso de falha."""
        try:
            with self._hw_lock:
                res = self._op.get_boot_status()
            if not res.get("error") and res.get("data"):
                return str(res["data"]["version"])
        except Exception as e:
            hardware_logger.error(f"Erro ao ler versão de firmware: {e}")
            self._connected = False
        return "N/A"
