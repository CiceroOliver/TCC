from __future__ import annotations
import os
import time
import threading
from math import ceil
from typing import Dict, Callable, Optional
from nascerr_modules.util.crc import calculate_stm32_compatible_sha256
from nascerr_modules.nascerr_operation import Operation
from nascerr_logging.mylogger import get_logger

logger = get_logger("hardware")

class FirmwareUpdater:
    def __init__(self, op: Operation, hw_lock: threading.Lock) -> None:
        """
        :param op: Instância da classe Operation que contém os métodos de baixo nível (boot_init, etc)
        :param hw_lock: Lock compartilhado para garantir exclusividade no barramento I2C
        """
        self._op = op
        self._hw_lock = hw_lock
        self._timeout_sec = 10.0 # TIMEOUT_SEC
        self._sleep_time = 0.1   # SLEEP_TIME

    def execute_update(self, 
                       firmware_data: bytes, 
                       new_version: int, 
                       packet_size: int = 256, 
                       force: bool = False) -> Dict:
        """
        Versão adaptada do seu método para trabalhar com bytes em memória
        e respeitar o Lock do hardware.
        """
        report: Dict = {"steps": [], "success": False}
        
        total_size = len(firmware_data)
        num_pkts = ceil(total_size / packet_size)
        
        # 1. Cálculo do Hash para validação no STM32
        sha256_hash = calculate_stm32_compatible_sha256(firmware_data)

        with self._hw_lock:
            try:
                # --- PASSO 1: VERIFICAR STATUS ATUAL ---
                stat = self._op.communication.boot_get_status()
                curr_ver = stat.get("data", {}).get("version_raw")
                logger.info(f"Versão atual do firmware: {curr_ver}, Nova versão: {new_version}")
                if curr_ver == new_version and not force:
                    logger.info(f"Update ignorado: Versão {curr_ver} já instalada.")
                    return {"skipped": True, "message": f"already at version {curr_ver}"}
                logger.info("Iniciando processo de atualização de firmware...")
                # --- PASSO 2: INICIALIZAR DOWNLOAD ---
                init = self._op.communication.boot_init(
                    firmware_size=total_size, 
                    version=new_version, 
                    num_packets=num_pkts
                )
                if init.get("error"):
                    logger.error(f"Erro no boot_init: {init.get('message')}")
                    return {"error": True, "message": "Falha no boot_init", "details": init}
                logger.info("Bootloader preparado para download. Aguardando modo download...")
                # --- PASSO 3: ESPERAR ESTABILIZAÇÃO DO BOOTLOADER ---
                if not self._wait_for_flag(flag="IN_DONWLOAD", timeout=5):
                    logger.error("Timeout esperando o hardware entrar no modo download.")
                    return {"error": True, "message": "Timeout esperando modo download"}
                logger.info("Hardware no modo download. Iniciando transferência de firmware...")
                # --- PASSO 4: TRANSFERÊNCIA DE DADOS ---
                for idx in range(1, num_pkts + 1):
                    chunk = firmware_data[(idx-1)*packet_size : idx*packet_size]
                    
                    tx = self._op.communication.boot_data_transfer(id_pkg=idx, data=chunk)
                    if tx.get("error"):
                        return {"error": True, "message": f"Erro no pacote {idx}", "step": "transfer"}

                    # Espera o hardware confirmar o processamento do pacote
                    if not self._wait_for_packet_ack(idx, timeout=2):
                        return {"error": True, "message": f"Timeout ACK pacote {idx}"}

                # --- PASSO 5: FINALIZAR ---
                fin = self._op.communication.boot_donwload_finish(new_version, sha=sha256_hash)
                
                if fin.get("error"):
                    return {"error": True, "message": "Erro ao finalizar download"}

                # --- PASSO 6: MONITORAR REBOOT ---
                success_confirm = self._confirm_update(new_version)
                
                return {
                    "success": success_confirm,
                    "version": new_version,
                    "total_size": total_size
                }

            except Exception as e:
                logger.error(f"Exceção durante update: {e}")
                return {"error": True, "message": str(e)}

    def _wait_for_flag(self, flag: str, timeout: float) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            st = self._op.communication.boot_get_status()
            flags = st.get("data", {}).get("flags", [])
            if flag in flags:
                return True
            time.sleep(self._sleep_time)
        return False

    def _wait_for_packet_ack(self, expected_id: int, timeout: float) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            st = self._op.communication.boot_get_status()
            data = st.get("data", {})
            if data.get("last_packet_id") == expected_id and "DOWNLOAD_SUCCESS" in data.get("flags", []):
                return True
            time.sleep(self._sleep_time)
        return False

    def _confirm_update(self, expected_version: int) -> bool:
        """Monitora a saída do bootloader e a nova versão."""
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                st = self._op.communication.boot_get_status()
                if not st.get("error"):
                    flags = st["data"].get("flags", [])
                    ver = st["data"].get("version_raw")
                    if "IN_BOOTLOADER" not in flags and ver == expected_version:
                        return True
            except:
                pass
            time.sleep(0.5)
        return False