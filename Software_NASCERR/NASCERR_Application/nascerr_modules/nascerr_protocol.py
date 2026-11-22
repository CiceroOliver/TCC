import struct
from time import sleep
from nascerr_modules.nascerr_command import *
from nascerr_modules.nascerr_response import *
from nascerr_modules.nascerr_system import *
from nascerr_modules.nascerr_hardware import *
from nascerr_modules.nascerr_registers import registers, AccessType
from nascerr_modules.util.crc import calculate_crc16, verify_crc16
from nascerr_modules.util.i2c_interface import I2CInterface
from nascerr_modules.nascerr_bootloader import NASCERR_BOOT, NASCERR_STATUS_BOOT, BootCommand
from nascerr_modules.nascerr_gps import NASCERR_GPS

count: int = 0
list_count: list[int] = []


class Protocol:
    def __init__(self, device_name, i2c_address):
        self.device_name = device_name
        self.i2c = I2CInterface(addr=i2c_address)

    def set_register(self, register_name, data):
        register = registers.get(register_name)

        if not register:
            raise ValueError(f"Register '{register_name}' does not exist.")

        if register.access not in [AccessType.WRITE_ONLY, AccessType.FULL_ACCESS]:
            raise PermissionError(
                f"Write access denied for register '{register_name}'.")

        if len(data) > register.size:
            raise ValueError(
                f"Data length exceeds the maximum size of {register.size} bytes for register '{register_name}'.")

        result = self.i2c.write_data(register.addr, data)

        return result

    def get_register(self, register_name):
        register = registers.get(register_name)

        if not register:
            raise ValueError(f"Register '{register_name}' does not exist.")

        if register.access not in [AccessType.READ_ONLY, AccessType.FULL_ACCESS]:
            raise PermissionError(
                f"Read access denied for register '{register_name}'.")

        data = self.i2c.read_data(register.addr, register.size)

        return data

    def get_register_withsize(self, register_name, size):
        register = registers.get(register_name)

        if not register:
            raise ValueError(f"Register '{register_name}' does not exist.")

        if register.access not in [AccessType.READ_ONLY, AccessType.FULL_ACCESS]:
            raise PermissionError(
                f"Read access denied for register '{register_name}'.")

        # Usando o 'size' fornecido em vez de 'register.size'
        data = self.i2c.read_data(register.addr, size)
        return data

    def get_status(self):
        status_register_name = "STAT"
        data_status = self.get_register(status_register_name)

        if isinstance(data_status["data"], list):
            status_obj = NASCERR_STATUS.from_list(data_status["data"][:-2])
            hex_data = ''.join(format(x, '02X') for x in data_status["data"])

            verify_crc = verify_crc16(hex_data)
            if not verify_crc:
                raise ValueError("Invalid CRC in STAT register data.")
            else:
                # Remover os dois últimos bytes (CRC) dos dados antes de processá-los
                data_status["data"] = data_status["data"][:-2]
            status_obj = NASCERR_STATUS.from_list(data_status["data"])
        else:
            raise ValueError(
                "Data read from register is not in expected format.")

        return {
            "error": data_status["error"],
            "message": data_status["message"],
            "data": status_obj.to_dict()
        }

    def boot_get_status(self):
        """
        Requests and parses the BOOT_STAT register. Returns a dict:
          {
            "error":   bool,
            "message": str,
            "data":    { "version":…, "flags":…, "last_error":… }
          }
        """
        register_name = "BOOT_STAT"

        resp = self.get_register(register_name)
        if resp["error"]:
            # pass through any bus error
            return resp

        data = resp["data"]
        if not isinstance(data, list) or len(data) < 15:
            raise ValueError("BOOT_STAT: expected 15 bytes of data, got "
                             f"{len(data) if isinstance(data, list) else type(data)}")

        hex_data = ''.join(format(x, '02X') for x in data)
        verify_crc = verify_crc16(hex_data)
        if not verify_crc:
            raise ValueError("Invalid CRC in BOOT_STAT register data.")

        raw_bytes = data[:13]
        boot_obj = NASCERR_STATUS_BOOT.from_list(raw_bytes)

        return {
            "error": resp["error"],
            "message": resp["message"],
            "data": boot_obj.to_dict()
        }

    def boot_init(self, firmware_size: int, version: int, num_packets: int):
        """ Initializes the bootloader for firmware download.
        This function sends a command to the device to prepare for firmware download.
        It sets the firmware size and version, and prepares the device for the download process
        """
        register_name = "BOOT_DATA"

        # Create a NASCERR_BOOT command for download initialization
        boot_ctrl = NASCERR_BOOT(
            cmd=BootCommand.DOWNLOAD_INIT
        )
        # Set the firmware size and version in the command
        boot_init_ctrl = boot_ctrl.make_download_init(
            fw_size=firmware_size, version=version, num_packets=num_packets)

        boot_init_bytes_info = boot_init_ctrl.to_bytes()
        boot_init_bytes = boot_init_bytes_info["message_bytes"]
        crc16 = boot_init_bytes_info["crc16"]

        result = self.set_register(register_name, boot_init_bytes)

        return {
            "error": result["error"],
            "message": result["message"],
            "command_bytes": boot_init_bytes.hex(),
            "crc16": f"{crc16:04X}",
            "command_dict": boot_init_ctrl.to_dict()
        }

    def boot_data_transfer(self, id_pkg: int, data: bytes):
        """ Transfers a chunk of data to the bootloader.
        This function sends a chunk of data to the device as part of the firmware download process.
        It is used to send the actual firmware data after the download initialization.
        """
        register_name = "BOOT_DATA"

        # Create a NASCERR_BOOT command for download initialization
        boot_ctrl = NASCERR_BOOT(
            cmd=BootCommand.DATA_TRANSFER
        )

        boot_init_ctrl = boot_ctrl.make_data_transfer(
            packet_id=id_pkg, chunk=data)
        boot_init_bytes_info = boot_init_ctrl.to_bytes()
        boot_init_bytes = boot_init_bytes_info["message_bytes"]
        crc16 = boot_init_bytes_info["crc16"]

        result = self.set_register(register_name, boot_init_bytes)

        return {
            "error": result["error"],
            "message": result["message"],
            "command_bytes": boot_init_bytes.hex(),
            "crc16": f"{crc16:04X}",
            "command_dict": boot_init_ctrl.to_dict()
        }

    def boot_donwload_finish(self, version: int, sha: bytes):
        """ Finalizes the bootloader download process.
        This function sends a command to the device to finalize the firmware download.
        It is used to complete the firmware update process after all data has been sent.
        """
        register_name = "BOOT_DATA"

        # Create a NASCERR_BOOT command for download initialization
        boot_ctrl = NASCERR_BOOT(
            cmd=BootCommand.DOWNLOAD_FINISH
        )

        boot_init_ctrl = boot_ctrl.make_download_finish(
            version=version, sha=sha)
        boot_init_bytes_info = boot_init_ctrl.to_bytes()
        boot_init_bytes = boot_init_bytes_info["message_bytes"]
        crc16 = boot_init_bytes_info["crc16"]

        result = self.set_register(register_name, boot_init_bytes)

        return {
            "error": result["error"],
            "message": result["message"],
            "command_bytes": boot_init_bytes.hex(),
            "crc16": f"{crc16:04X}",
            "command_dict": boot_init_ctrl.to_dict()
        }

    def set_commandNew(
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
        Envia um comando NASCERR_CMD para o registrador CMD.

        Parâmetros correspondem aos campos do NASCERR_CMD.
        """
        # Monta o comando
        cmd = NASCERR_CMD(
            cmd_type=cmd_type,
            id=id,
            block=block,
            qtd_block=qtd_block,
            ecc=ecc,
            write_type=write_type,
            scheduler_time=scheduler_time,
            repeat=repeat,
            delay=delay,
            current_tick=current_tick
        )

        # Serializa
        cmd_info = cmd.to_bytes()
        command_bytes = cmd_info["message_bytes"]
        crc16 = cmd_info["crc16"]

        # Grava no registrador “CMD”
        result = self.set_register("CMD", command_bytes)

        return {
            "error":         result["error"],
            "message":       result["message"],
            "command_bytes": command_bytes.hex(),
            "crc16":         f"{crc16:04X}",
            "command_dict":  cmd.to_dict()
        }

    def get_experiment_status(self):
        register = "EXP_STAT"
        resp = self.get_register(register)

        if not isinstance(resp["data"], list):
            raise ValueError(
                "Data read from register is not in expected format.")

        # monta string hex para CRC
        hex_str = "".join(f"{b:02X}" for b in resp["data"])
        if not verify_crc16(hex_str):
            raise ValueError(f"Invalid CRC in {register} register data.")

        # remove os 2 bytes de CRC e converte em bytes
        payload = bytes(resp["data"][:-2])
        exp_status = NASCERR_EXP_STATUS.from_bytes(payload)

        return {
            "error":   resp.get("error"),
            "message": resp.get("message"),
            "data":    exp_status.to_dict()
        }

    def set_expSumIDX(self, offset: int) -> dict:
        """
        Write <offset> to the EXP_SUM_IDX register.

        • offset precisa ser múltiplo de 30 (tamanho de página)  
        • offset ≤ 255 (um único byte, segundo o firmware)

        Retorna o dicionário padrão da sua `set_register`.
        """
        register = "EXP_SUM_IDX"

        if offset % 30 != 0:
            raise ValueError("offset deve ser múltiplo de 30 (0, 30, 60 …)")
        if not 0 <= offset <= 0xFF:
            raise ValueError("offset deve caber em 1 byte (0-255)")

        message = struct.pack('<B', offset)

        crc16 = calculate_crc16(message)
        message += struct.pack('>H', crc16)

        result = self.set_register(register, message)

        return {
            "error":   result["error"],
            "message": result["message"],
            "data":    {
                "offset": offset,
                "crc16":  f"{crc16:04X}"
            }
        }

    def get_expSUM(self) -> dict:
        """
        Read EXP_SUM_DATA, valida CRC16 e devolve objeto NASCERR_EXP_SUMMARY.

        Use depois de chamar `set_expSumIDX()`.
        """
        register = "EXP_SUM_DATA"
        resp = self.get_register(register)
        # print("resp: ", resp)
        if not isinstance(resp["data"], list):
            raise ValueError(
                "Dados de EXP_SUM_DATA não estão no formato esperado")

        # CRC16: ultimo par de bytes
        hex_str = "".join(f"{b:02X}" for b in resp["data"])
        if not verify_crc16(hex_str):
            raise ValueError(f"CRC inválido em {register}")

        # Remove CRC (2 bytes finais) e faz parse
        payload = bytes(resp["data"][:-2])
        summary = NASCERR_EXP_SUMMARY.from_bytes(payload)

        return {
            "error":   resp.get("error"),
            "message": resp.get("message"),
            "data":    summary.to_dict(),
        }

    def get_temperature(self):
        """
        Le o registrador HW_STAT e retorna os valores de temperature
        via HW_TXB.
        """
        hardware = NASCERR_HARDWARE("temperature")
        hw_stat_register_name = "HW_STAT"
        payload = hardware.write_hw_status_payload()
        write = self.set_register(hw_stat_register_name, payload["data"])

        if write["error"]:
            return {
                "error": True,
                "message": write["message"],
                "data": None
            }
        sleep(0.05)

        hw_stat_register_name = "HW_TXB"
        buff_size = hardware.get_size_buff_tx()
        result = self.get_register_withsize(hw_stat_register_name, buff_size)
        if result["error"]:
            return {
                "error": True,
                "message": result["message"],
                "data": None
            }

        return {
            "error": False,
            "message": result["message"],
            "data": hardware.read_decode_hw_status(result)
        }

    def get_total_current(self):
        """
       Le o registrador HW_STAT e retorna os valores de total_current via HW_TXB.
        """
        hardware = NASCERR_HARDWARE("current")
        hw_stat_register_name = "HW_STAT"
        payload = hardware.write_hw_status_payload()
        write = self.set_register(hw_stat_register_name, payload["data"])

        if write["error"]:
            return {
                "error": True,
                "message": write["message"],
                "data": None
            }
        sleep(0.05)

        hw_stat_register_name = "HW_TXB"
        buff_size = hardware.get_size_buff_tx()
        result = self.get_register_withsize(hw_stat_register_name, buff_size)
        if result["error"]:
            return {
                "error": True,
                "message": result["message"],
                "data": None
            }

        valor = sum(hardware.read_decode_hw_status(result))
        return {
            "error": False,
            "message": result["message"],
            "data": [valor]
        }

    def get_current(self):
        """
        Le o registrador HW_STAT e retorna os valores de current via HW_TXB.
        """
        hardware = NASCERR_HARDWARE("current")
        hw_stat_register_name = "HW_STAT"
        payload = hardware.write_hw_status_payload()
        write = self.set_register(hw_stat_register_name, payload["data"])

        if write["error"]:
            return {
                "error": True,
                "message": write["message"],
                "data": None
            }
        sleep(0.05)

        hw_stat_register_name = "HW_TXB"
        buff_size = hardware.get_size_buff_tx()
        result = self.get_register_withsize(hw_stat_register_name, buff_size)
        if result["error"]:
            return {
                "error": True,
                "message": result["message"],
                "data": None
            }

        return {
            "error": False,
            "message": result["message"],
            "data": hardware.read_decode_hw_status(result)
        }

    def get_total_power(self):
        """
        Le o registrador HW_STAT e retorna os valores de power total
        via HW_TXB.
        """
        hardware = NASCERR_HARDWARE("total_power")
        hw_stat_register_name = "HW_STAT"
        payload = hardware.write_hw_status_payload()
        write = self.set_register(hw_stat_register_name, payload["data"])

        if write["error"]:
            return {
                "error": True,
                "message": write["message"],
                "data": None
            }
        sleep(0.05)

        hw_stat_register_name = "HW_TXB"
        buff_size = hardware.get_size_buff_tx()
        result = self.get_register_withsize(hw_stat_register_name, buff_size)
        if result["error"]:
            return {
                "error": True,
                "message": result["message"],
                "data": None
            }

        return {
            "error": False,
            "message": result["message"],
            "data": hardware.read_decode_hw_status(result)
        }

    def get_power(self):
        """
        Le o registrador HW_STAT e retorna os valores de power via HW_TXB.
        """
        hardware = NASCERR_HARDWARE("power")
        hw_stat_register_name = "HW_STAT"
        payload = hardware.write_hw_status_payload()
        write = self.set_register(hw_stat_register_name, payload["data"])

        if write["error"]:
            return {
                "error": True,
                "message": write["message"],
                "data": None
            }
        sleep(0.05)

        hw_stat_register_name = "HW_TXB"
        buff_size = hardware.get_size_buff_tx()
        result = self.get_register_withsize(hw_stat_register_name, buff_size)
        if result["error"]:
            return {
                "error": True,
                "message": result["message"],
                "data": None
            }

        return {
            "error": False,
            "message": result["message"],
            "data": hardware.read_decode_hw_status(result)
        }

    def set_sleep(self):
        """
        Coloque o dispositivo em modo de Sleep.
        """
        power_manager = NASCERR_Power_Manager()
        payload = power_manager.sleep()

        result = self.set_register("SLEEP", payload)

        if result["error"]:
            return {
                "error": True,
                "message": result["message"],
                "data": None
            }

        return result

    def set_wakeup(self):
        """
        Acorde o dispositivo do modo de Sleep.
        """
        power_manager = NASCERR_Power_Manager()
        payload = power_manager.wakeup()

        result = self.set_register("WAKEUP", payload)

        if result["error"]:
            return {
                "error": True,
                "message": result["message"],
                "data": None
            }

        return result

    def get_experiment(self) -> dict:
        """
        Lê o(s) resultado(s) de experimento(s) presente(s) no dispositivo.

        Passos:
        1. IDX – 2 bytes de tamanho + 2 bytes de CRC16
        2. TXB – lê exatamente <tamanho> bytes
        3. Valida CRC16 de ambos os registros
        4. Remove CRC de TXB, fatia o payload em blocos de
        NASCERR_EXPERIMENT._SIZE (= 86 bytes) e desserializa
        5. Devolve lista de dicionários

        Não há envio de ACK; em caso de erro gera ValueError.
        """
        sleep(0.01)

        # ---------- IDX ----------
        idx_resp = self.get_register("IDX")
        if idx_resp["error"]:
            return idx_resp

        if len(idx_resp["data"]) != 4:
            raise ValueError("IDX: esperado 4 bytes (size+CRC)")

        if not verify_crc16("".join(f"{b:02X}" for b in idx_resp["data"])):
            raise ValueError("IDX: CRC inválido")

        exp_len = struct.unpack('<H', bytes(idx_resp["data"][:2]))[0]
        # print(f"Expecting {exp_len} bytes in TXB")
        # ---------- TXB ----------
        txb_resp = self.get_register_withsize("TXB", exp_len)
        # print("data read: ", txb_resp)
        if txb_resp["error"]:
            return txb_resp

        if len(txb_resp["data"]) != exp_len:
            raise ValueError(
                "TXB: numero de bytes diferente do indicado em IDX")

        if not verify_crc16("".join(f"{b:02X}" for b in txb_resp["data"])):
            raise ValueError("TXB: CRC invalido")

        # Remove CRC16 final e desserializa
        payload = bytes(txb_resp["data"][:-2])
        blk_size = NASCERR_EXPERIMENT._SIZE
        if len(payload) % blk_size != 0:
            raise ValueError(
                "TXB: payload noo e multiplo de NASCERR_EXPERIMENT")

        experiments = [
            NASCERR_EXPERIMENT.from_bytes(payload[i:i + blk_size])
            for i in range(0, len(payload), blk_size)
        ]

        return {
            "error":   False,
            "message": txb_resp["message"],
            "data":    [exp.to_dict() for exp in experiments]
        }

    def clear_database(self, region: FlashRegionTypeDef) -> dict:
        """
        Escreve em DB_CLEAR para apagar uma região da flash.

        • `region` deve ser um membro de FlashRegionTypeDef  
        • Payload: 1 byte (região) + 2 bytes CRC16 little-endian
        """
        if not isinstance(region, FlashRegionTypeDef):
            raise TypeError("region precisa ser FlashRegionTypeDef")

        if region == FlashRegionTypeDef.MAX_FLASH_REGION:
            raise ValueError(
                "MAX_FLASH_REGION nao e regiao valida para apagar")

        register = "DB_CLEAR"

        payload = struct.pack("<B", region.value)
        crc16 = calculate_crc16(payload)
        payload += struct.pack(">H", crc16)

        result = self.set_register(register, payload)

        return {
            "error":   result["error"],
            "message": result["message"],
            "data": {
                "region": region.name,
                "value":  region.value,
                "crc16":  f"{crc16:04X}"
            }
        }

    def send_gps_data(self) -> dict:
        """
        Grava coordenadas aleatorias no registrador GPS, seguidos de CRC16.
        tamanho fixo: 34 bytes (GNSS) + 2 bytes de CRC (=36)
        """
        register = "GPS"

        gps = NASCERR_GPS()
        gps._fill_random_fields()

        result = self.set_register(register, gps.to_bytes())

        return {
            "error":   result["error"],
            "message": result["message"],
            "data": gps.to_bytes().hex(':')
        }

    def set_log_page(self, page: int) -> dict:
        """
        Escreve o número da página no registrador LOG_SET_PAGE.
        """
        register = "LOG_SET_PAGE"
        if not 0 <= page <= 255:
            raise ValueError("O número da página deve estar entre 0 e 255.")

        payload = struct.pack('<B', page)
        crc16 = calculate_crc16(payload)
        # Firmware espera CRC em Big Endian
        message = payload + struct.pack('>H', crc16)

        # print(f"DEBUG PYTHON (set_log_page): Payload={payload.hex(' ')}, CRC=0x{crc16:04X}, Mensagem Final={message.hex(' ')}") # TODO: print aqui nao :/

        result = self.set_register(register, message)

        return {
            "error":   result["error"],
            "message": result["message"],
            "data": {"page": page, "crc16": f"{crc16:04X}"}
        }

    def get_log_summary(self) -> dict:
        """
        Lê o registrador LOG_SUM, valida e retorna o sumário.
        """
        register = "LOG_SUM"
        resp = self.get_register(register)

        if resp.get("error"):
            return resp

        # Valida o CRC (payload de 14 bytes + 2 de CRC)
        hex_str = "".join(f"{b:02X}" for b in resp["data"])
        if not verify_crc16(hex_str):
            raise ValueError(f"CRC inválido no registrador {register}")

        payload = bytes(resp["data"][:-2])
        summary = NASCERR_LOG_SUMMARY.from_bytes(payload)

        return {
            "error":   False,
            "message": resp.get("message"),
            "data":    summary.to_dict(),
        }

    def get_log_page(self) -> dict:
        """
        Lê uma página de logs do dispositivo usando o método IDX+TXB.
        """
        sleep(0.01)

        # 1. Lê o registrador IDX para saber o tamanho do pacote
        idx_resp = self.get_register("IDX")
        if idx_resp["error"]:
            return idx_resp

        if len(idx_resp["data"]) != 4:
            raise ValueError("IDX: esperado 4 bytes (tamanho+CRC)")

        if not verify_crc16("".join(f"{b:02X}" for b in idx_resp["data"])):
            raise ValueError("IDX: CRC inválido")

        payload_len_with_crc = struct.unpack(
            '<H', bytes(idx_resp["data"][:2]))[0]

        # Se não há logs, retorna uma lista vazia
        if payload_len_with_crc <= 2:
            return {"error": False, "message": "Página de log vazia.", "data": []}

        # 2. Lê exatamente esse tamanho do registrador LOG_READ_PAGE
        txb_resp = self.get_register_withsize(
            "LOG_READ_PAGE", payload_len_with_crc)

        # # ==========================================================
        # #          INÍCIO DO CÓDIGO DE DEPURAÇÃO
        # # ==========================================================
        # print("\n--- DEBUG PYTHON: Dados recebidos ---")
        # print(f"Tamanho esperado (lido do IDX): {payload_len_with_crc} bytes")

        # received_bytes = txb_resp.get("data", [])
        # if received_bytes:
        #     # Imprime o buffer em formato hexadecimal, 16 bytes por linha
        #     print("Conteúdo bruto recebido do LOG_READ_PAGE:")
        #     for i in range(0, len(received_bytes), 16):
        #         chunk = received_bytes[i:i+16]
        #         hex_string = " ".join(f"{byte:02X}" for byte in chunk)
        #         print(f"  LOG_READ_PAGE[{i:04d}]: {hex_string}")
        # else:
        #     print("Nenhum dado recebido no LOG_READ_PAGE.")
        # print("------------------------------------")
        # # ==========================================================
        # #          FIM DO CÓDIGO DE DEPURAÇÃO
        # # ==========================================================

        if txb_resp["error"]:
            return txb_resp

        if len(txb_resp["data"]) != payload_len_with_crc:
            raise ValueError(
                "LOG_READ_PAGE: número de bytes diferente do indicado em IDX")

        if not verify_crc16("".join(f"{b:02X}" for b in txb_resp["data"])):
            raise ValueError("LOG_READ_PAGE: CRC inválido")

        # 3. Processa o payload
        payload = bytes(txb_resp["data"][:-2])
        log_size = 7  # Tamanho de cada struct NASCERR_LOG em bytes

        if len(payload) % log_size != 0:
            raise ValueError(
                "LOG_READ_PAGE: payload de log não é múltiplo de 7")

        # A função parse_multiple que já existe em NASCERR_LOG é perfeita para isso
        logs = NASCERR_LOG.parse_multiple(payload)

        return {
            "error":   False,
            "message": txb_resp["message"],
            "data":    [log.to_dict() for log in logs]
        }

    def get_ack_phase(self):
        register = "ACK"
        resp = self.get_register(register)

        if not isinstance(resp["data"], list):
            raise ValueError(
                "Data read from register is not in expected format.")

        # monta string hex para CRC
        hex_str = "".join(f"{b:02X}" for b in resp["data"])
        if not verify_crc16(hex_str):
            raise ValueError(f"Invalid CRC in {register} register data.")

        # remove os 2 bytes de CRC e converte em bytes
        payload = int.from_bytes(bytes(resp["data"][:-2]), byteorder='little')

        exp_status = NASCERR_ACK(payload)

        return {
            "error":   resp.get("error"),
            "message": resp.get("message"),
            "data":    exp_status.to_dict()
        }

    # TODO: IMPLEMENTAR METODO PARA LEITURA DO REGISTRO DE IMAGEM
    def get_image_status(self):
        register = "IMAGE_STAT"
        resp = self.get_register(register)

        if not isinstance(resp["data"], list):
            raise ValueError(
                "Data read from register is not in expected format.")

        # monta string hex para CRC
        hex_str = "".join(f"{b:02X}" for b in resp["data"])
        if not verify_crc16(hex_str):
            raise ValueError(f"Invalid CRC in {register} register data.")

        # remove os 2 bytes de CRC e converte em bytes
        payload = bytes(resp["data"][:-2])
        image_status = NASCERR_IMAGE_STATUS.from_bytes(payload)

        return {
            "error":   resp.get("error"),
            "message": resp.get("message"),
            "data":    image_status.to_dict()
        }
