from time import sleep
from nascerr_modules.nascerr_protocol import Protocol, NASCERR_CMD
from nascerr_modules.nascerr_system import *
from typing import List, Dict
import os
import time
from nascerr_modules.util.crc import calculate_stm32_compatible_sha256
from math import ceil

SLEEP_TIME = 0.05
CONVERT_TIME = 1
# restrições de envio
MAX_WORD = 1024
MAX_REP = 1  # não utilizado -> verificar necessidade
TIMEOUT_SEC = 20.0  # tempo máximo que vamos esperar por cada pacote


class Operation:
    def __init__(self, device_name, i2c_address):
        self.communication = Protocol(
            device_name=device_name, i2c_address=i2c_address)

    def get_status(self):
        # sleep(SLEEP_TIME)
        try:
            result = self.communication.get_status()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }

        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
                # AINDA NÃO VI UMA APLICACAO DISSO (TALVEZ QUANDO FOR O OBDH MSM)
                "connection_status": "tentando"
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "data": result["data"],
            "connection_status": "conectado"
        }

    def send_commandNew(
            self,
            cmd_type,
            id,
            block,
            qtd_block,
            ecc,
            write_type,
            scheduler_time,
            repeat,
            delay,
            current_tick):
        """
        Envia um comando NASCERR_CMD.

        Parâmetros iguais aos campos do NASCERR_CMD.
        """
        # Monta o objeto de comando
        cmd = NASCERR_CMD(
            cmd_type=cmd_type,
            id=id,
            block=block,
            qtd_block=qtd_block,
            ecc=ecc,
            write_type=write_type,
            scheduler_time=scheduler_time * CONVERT_TIME,  # min → ms
            repeat=repeat,
            delay=delay,
            current_tick=current_tick
        )

        # Envia pelo canal de comunicacao
        result = self.communication.set_commandNew(
            cmd.cmd_type,
            cmd.id,
            cmd.block,
            cmd.qtd_block,
            cmd.ecc,
            cmd.write_type,
            cmd.scheduler_time,
            cmd.repeat,
            cmd.delay,
            cmd.current_tick
        )

        sleep(SLEEP_TIME)

        if result.get("error"):
            return {
                "error":  result["error"],
                "status": "fail",
                "log_error": result["message"]
            }

        return {
            "error":   result["error"],
            "message": result["message"],
            "command_bytes": result.get("command_bytes", b""),
            "crc16":   result.get("crc16", 0),
            "command_dict": result.get("command_dict", {})
        }

    def get_experiment_status(self):
        try:
            result = self.communication.get_experiment_status()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }

        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
                "connection_status": "tentando"
            }

        return {
            "error":             result["error"],
            "message":           result["message"],
            "data":              result["data"],
            "connection_status": "conectado"
        }

    def set_exp_sum_idx(self, offset: int):
        try:
            result = self.communication.set_expSumIDX(offset)
        except ValueError as e:
            return {
                "error": True,
                "status": "fail",
                "log_error": str(e)
            }

        if result.get("error"):
            return {
                "error":  result["error"],
                "status": "fail",
                "log_error": result["message"]
            }

        return {
            "error":   result["error"],
            "status":  "ok",
            "message": result["message"]
        }

    def get_exp_sum(self):
        try:
            result = self.communication.get_expSUM()
        except ValueError as e:
            return {
                "error": True,
                "status": "fail",
                "log_error": str(e),
            }

        if result.get("error"):
            return {
                "error":  result["error"],
                "status": "fail",
                "log_error": result["message"],
                "data":    result["data"]
            }

        return {
            "error":   result["error"],
            "status":  "ok",
            "message": result["message"],
            "data":    result["data"]
        }

    def update_firmware(self, firmware_path: str,
                        new_version: int,
                        packet_size: int = 256,
                        progress_cb=False,
                        force: bool = 0) -> Dict:
        """
        Atualiza o firmware do dispositivo.

        :param firmware_path: Caminho para o arquivo de firmware.
        :param new_version: Nova versão do firmware.
        :param packet_size: Tamanho do pacote de dados para envio. 
        :param progress_cb: Função de callback para reportar progresso (opcional).
        :param force: Forçar atualização mesmo se a versão atual for igual ou maior que a nova.
        :return: Dicionário com o relatório da atualização.
        """
        report: Dict = {"steps": []}

        if not os.path.isfile(firmware_path):
            raise FileNotFoundError(f"Firmware not found: {firmware_path}")
        fw = open(firmware_path, "rb").read()

        sha256_hash = calculate_stm32_compatible_sha256(fw)

        total_size = len(fw)
        num_pkts = ceil(total_size / (packet_size))

        stat = self.communication.boot_get_status()
        report["steps"].append({"action": "get_status", "result": stat})

        curr_ver = stat["data"]["version_raw"]
        if curr_ver == new_version and not force:
            report["skipped"] = f"already at version {curr_ver}"
            return report

        init = self.communication.boot_init(
            firmware_size=total_size, version=new_version, num_packets=num_pkts)
        report["steps"].append({"action": "init_download", "result": init})
        if init["error"]:
            return report

        start_total = time.time()
        start_init = time.time()
        init_stabilized = False
        last_flags = stat["data"]["flags"]

        while time.time() - start_init < TIMEOUT_SEC:
            st_init = self.communication.boot_get_status()
            sleep(SLEEP_TIME)
            report["steps"].append(
                {"action": "check_init_status", "result": st_init})
            if st_init["error"]:
                return report

            flags = st_init["data"]["flags"]

            if flags != last_flags:
                last_flags = flags
                init_stabilized = True

            if "IN_DONWLOAD" in flags:
                break

            if init_stabilized and "ERROR_DETECTED" in flags:
                return {
                    "error": True,
                    "message": "Device reported error after init",
                    "post_init_status": st_init
                }

            time.sleep(SLEEP_TIME)

        else:
            return {
                "error": True,
                "message": "Timeout waiting for device to enter bootloader mode",
                "post_init_status": st_init
            }

        step = {"action": "data_transfer", "packets": []}
        report["steps"].append(step)

        total_packets = num_pkts
        retransmitted_packets = 0
        successful_packets = 0

        for idx in range(1, num_pkts + 1):
            chunk = fw[(idx-1)*packet_size: idx*packet_size]
            tx = self.communication.boot_data_transfer(id_pkg=idx, data=chunk)

            entry = {"id": idx, "tx_result": tx}
            start = time.time()
            if tx["error"]:
                report["error_at"] = idx
                step["packets"].append(entry)
                retransmitted_packets += 1
                continue

            while True:
                st = self.communication.boot_get_status()
                sleep(SLEEP_TIME)
                if st["error"]:
                    entry["status_error"] = (
                        f"Failed to read BOOT_STAT after packet {idx}: "
                        f"{st['message']}"
                    )
                    report["error_at"] = idx
                    step["packets"].append(entry)
                    return report

                entry["status_after"] = st["data"]
                flags = st["data"]["flags"]
                last_packet_id = st["data"]["last_packet_id"]

                if last_packet_id == idx and "DOWNLOAD_SUCCESS" in flags:
                    successful_packets += 1
                    break

                if "ERROR_DETECTED" in flags:
                    entry["status_error"] = f"Error flag raised after packet {idx}"
                    report["error_at"] = idx
                    step["packets"].append(entry)
                    return report

                if time.time() - start > TIMEOUT_SEC:
                    entry["status_error"] = f"Timeout waiting for pkt {idx}"
                    report["error_at"] = idx
                    step["packets"].append(entry)
                    return report

            step["packets"].append(entry)

        fin = self.communication.boot_donwload_finish(
            new_version, sha=sha256_hash)
        report["steps"].append({"action": "finish_download", "result": fin})
        if fin.get("error"):
            return {
                "error": True,
                "message": "Erro ao finalizar download",
                "finish_result": fin
            }

        monitor_step = {"action": "monitor_exit_bootloader", "reads": []}
        report["steps"].append(monitor_step)

        sleep(1)
        post_finish_deadline = time.time() + (TIMEOUT_SEC * 2)
        left_bootloader = False
        running_version = None
        last_err = None

        while time.time() < post_finish_deadline:
            try:
                st_pf = self.communication.boot_get_status()
            except ValueError as e:
                # Ignora erros de CRC durante a transição do bootloader → aplicação
                last_err = {"error": True, "message": str(e)}
                time.sleep(SLEEP_TIME)
                continue

            sleep(SLEEP_TIME)
            monitor_step["reads"].append(st_pf)

            if st_pf.get("error"):
                last_err = st_pf
                time.sleep(SLEEP_TIME)
                continue

            flags_pf = st_pf["data"]["flags"]
            running_version = st_pf["data"].get("version_raw", None)

            if "IN_BOOTLOADER" not in flags_pf:
                left_bootloader = True
                if running_version is not None:
                    break

            time.sleep(SLEEP_TIME)

        total_time = time.time() - start_total
        version_match = (running_version ==
                         new_version) if running_version is not None else False

        summary = {
            "version": new_version,
            "total_packets": total_packets,
            "retransmitted_packets": retransmitted_packets,
            "successful_packets": successful_packets,
            "total_firmware_size": total_size,
            "total_time_seconds": total_time,
            "left_bootloader_confirmed": left_bootloader,
            "running_version": running_version,
            "version_match": version_match,
            "success": ("error_at" not in report) and left_bootloader and version_match
        }

        if not summary["success"]:
            fail_report = {
                "error": True,
                "message": "Falha na confirmação pós-update",
                "left_bootloader": left_bootloader,
                "running_version": running_version,
                "expected_version": new_version,
                "last_status_error": last_err,
                "summary": summary,
                "report": report
            }
            if progress_cb:
                progress_cb(fail_report)
            return fail_report

        if progress_cb:
            progress_cb(report)

        return summary

    def get_boot_status(self):
        try:
            result = self.communication.boot_get_status()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }
        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
                "connection_status": "desconectado"
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "data": result["data"],
            "connection_status": "conectado"
        }

    def get_hardware_temperature(self):
        """
        Obtem a temperatura do hardware.
        """
        try:
            result = self.communication.get_temperature()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }
        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "data": result["data"],
            "connection_status": "conectado"
        }

    def get_hardware_current(self):
        """
        Obtem a corrente do hardware.
        """
        try:
            result = self.communication.get_current()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }
        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "data": result["data"],
            "connection_status": "conectado"
        }

    def get_hardware_power(self):
        """
        Obtem a potencia do hardware.
        """
        try:
            result = self.communication.get_power()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }
        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "data": result["data"],
            "connection_status": "conectado"
        }

    def get_hardware_total_current(self):
        """
        Obtem a corrente total do hardware.
        """
        try:
            result = self.communication.get_total_current()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }
        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "data": result["data"],
            "connection_status": "conectado"
        }

    def get_hardware_total_power(self):
        """
        Obtem a potencia total do hardware.
        """
        try:
            result = self.communication.get_total_power()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }
        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "data": result["data"],
            "connection_status": "conectado"
        }

    def system_sleep(self):
        """
        Coloca o sistema em modo de sleep.
        """
        try:
            result = self.communication.set_sleep()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }
        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "connection_status": "Sleep"
        }

    def system_wakeup(self):
        """
        Desperta o sistema do modo de sleep.
        """
        try:
            result = self.communication.set_wakeup()
        except ValueError as e:
            return {
                "error": "Device not connected",
                "status": "fail",
                "log_error": str(e),
                "connection_status": "desconectado"
            }
        if result.get("error"):
            return {
                "error": result["error"],
                "status": "fail",
                "log_error": result["message"],
                "connection_status": "desconectado"
            }

        return {
            "error": result["error"],
            "message": result["message"],
            "data": result["data"],
            "connection_status": "conectado"
        }

    def read_experiment(self):
        """
        Lê o(s) experimento(s) gravado(s) no dispositivo e devolve-os como
        lista de dicionários NASCERR_EXPERIMENT.

        Retorno:
            {
                "error":   bool       – True se ocorreu erro de barramento/CRC/etc.
                "status":  "ok" | "fail"
                "message": str        – mensagem do driver I²C
                "data":    list[dict] – lista com os experimentos (ou None em erro)
                "log_error": str      – descrição do erro caso status == "fail"
            }
        """
        try:
            result = self.communication.get_experiment()
        except Exception as e:
            return {
                "error": True,
                "status": "fail",
                "log_error": f"Exception occurred: {str(e)}",
                "data": None
            }

        sleep(SLEEP_TIME)

        if result is None:
            return {
                "error": True,
                "status": "fail",
                "log_error": "No response from the device (NoneType)",
                "data": None
            }

        if result.get("error"):
            return {
                "error":  result["error"],
                "status": "fail",
                "log_error": result["message"],
                "data": None
            }

        return {
            "error":   result["error"],
            "status":  "ok",
            "message": result["message"],
            "data":    result["data"]
        }

    def clear_database(self, region: FlashRegionTypeDef):
        """
        Apaga a região indicada na flash chamando Protocol.clear_database().

        Retorno:
            {
                "error":   bool
                "status":  "ok" | "fail"
                "message": str
                "data":    { "region": str, "value": int, "crc16": "ABCD" }
                "log_error": str   (apenas se status == "fail")
            }
        """
        try:
            resp = self.communication.clear_database(region)
        except (ValueError, TypeError) as e:
            return {
                "error": True,
                "status": "fail",
                "log_error": str(e)
            }

        if resp.get("error"):
            return {
                "error":  resp["error"],
                "status": "fail",
                "log_error": resp["message"]
            }

        return {
            "error":   False,
            "status":  "ok",
            "message": resp["message"],
            "data":    resp["data"]
        }

    def gps_data(self) -> dict:
        """
        Envia 34 bytes referentes aos dados de gps + CRC-16 para o registrador GPS.

        Retorno-padrão:
            {
                "error":   bool,
                "status":  "ok" | "fail",
                "message": str,
                "data":    bytes escritos,
                "log_error": str   # somente se status == "fail"
            }
        """
        try:
            resp = self.communication.send_gps_data()
        except Exception as exc:
            return {
                "error": True,
                "status": "fail",
                "log_error": f"Exception occurred: {exc}"
            }

        if resp.get("error"):
            return {
                "error":  resp["error"],
                "status": "fail",
                "log_error": resp["message"]
            }

        return {
            "error":  False,
            "status": "ok",
            "message": resp["message"],
            "data":    resp["data"]
        }

    def set_log_page(self, page: int) -> dict:
        """
        Define a página de logs a ser consultada no dispositivo.
        Chama Protocol.set_log_page().
        """
        try:
            # Chama o método da camada de comunicação
            result = self.communication.set_log_page(page)
            if result.get("error"):
                return {"error": True, "status": "fail", "log_error": result["message"]}

            return {
                "error": False,
                "status": "ok",
                "message": result["message"],
                "data": result["data"]
            }
        except Exception as e:
            return {"error": True, "status": "fail", "log_error": str(e)}

    def get_log_summary(self) -> dict:
        """
        Lê apenas o sumário da página de logs previamente selecionada.
        Chama Protocol.get_log_summary().
        """
        try:
            # Chama o método da camada de comunicação
            result = self.communication.get_log_summary()
            if result.get("error"):
                return {"error": True, "status": "fail", "log_error": result["message"]}

            return {
                "error": False,
                "status": "ok",
                "message": result["message"],
                "data": result["data"]
            }
        except Exception as e:
            return {"error": True, "status": "fail", "log_error": str(e)}

    def get_log_page_content(self) -> dict:
        """
        Lê o conteúdo completo da página de logs previamente selecionada.
        Chama Protocol.get_log_page_content().
        """
        try:
            # Chama o método da camada de comunicação
            result = self.communication.get_log_page()
            # result = self.communication.get_log_page_content()
            if result.get("error"):
                return {"error": True, "status": "fail", "log_error": result["message"]}

            return {
                "error": False,
                "status": "ok",
                "message": result["message"],
                "data": result["data"]
            }
        except Exception as e:
            return {"error": True, "status": "fail", "log_error": str(e), "data": None}

    def get_ack(self) -> dict:
        """
        Lê o ACK do dispositivo.
        Chama Protocol.get_ack_phase().
        """
        try:
            result = self.communication.get_ack_phase()
        except Exception as e:
            return {
                "error": True,
                "status": "fail",
                "log_error": f"Exception occurred: {str(e)}"
            }

        if result.get("error"):
            return {
                "error":  result["error"],
                "status": "fail",
                "log_error": result["message"]
            }

        return {
            "error":  False,
            "status": "ok",
            "message": result["message"],
            "data":    result["data"]
        }

    # TODO: IMPLEMENTAR METODO PARA LEITURA DO REGISTRO DE IMAGEM

    def get_image_status_content(self) -> dict:
        """
        Lê o conteúdo do registro de status de imagem.
        """
        try:
            result = self.communication.get_image_status()
        except Exception as e:
            return {
                "error": True,
                "status": "fail",
                "log_error": f"Exception occurred: {str(e)}"
            }

        if result.get("error"):
            return {
                "error":  result["error"],
                "status": "fail",
                "log_error": result["message"]
            }

        return {
            "error":  False,
            "status": "ok",
            "message": result["message"],
            "data":    result["data"]
        }
