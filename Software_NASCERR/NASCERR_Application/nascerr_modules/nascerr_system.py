import json
from enum import Enum, IntEnum
import struct


class OPERATION_EXP(Enum):
    """
    Enum for Operating Logs.
    """
    OP_EXP_OK = 0xA1
    OP_EXP_RUNNING = 0xA2
    OP_EXP_BUSY = 0xA3
    OP_EXP_FAIL = 0xA4

    @property
    def description(self):
        descriptions = {
            OPERATION_EXP.OP_EXP_OK: "No experiment in execution.",
            OPERATION_EXP.OP_EXP_RUNNING: "Experiment is running.",
            OPERATION_EXP.OP_EXP_BUSY: "System is busy.",
            OPERATION_EXP.OP_EXP_FAIL: "Operation failed.",
        }
        return descriptions.get(self, "Unknown status")


class REQUEST_EXP(Enum):
    """
    Enum for Request Logs.
    """
    REQ_EXP_NO_REQUEST = 0xA1
    REQ_EXP_CMD_RECEIVED = 0xA2
    REQ_EXP_READ_PROCESSING = 0xA3
    REQ_EXP_TX_RESULT = 0xA4
    REQ_EXP_FAIL = 0xA5

    @property
    def description(self):
        descriptions = {
            REQUEST_EXP.REQ_EXP_NO_REQUEST: "No request has been made.",
            REQUEST_EXP.REQ_EXP_CMD_RECEIVED: "Read CMD was received.",
            REQUEST_EXP.REQ_EXP_READ_PROCESSING: "Awating the data to be processed.",
            REQUEST_EXP.REQ_EXP_TX_RESULT: "Transmission result request.",
            REQUEST_EXP.REQ_EXP_FAIL: "A failed occured."
        }
        return descriptions.get(self, "Unknown request")


# Definição dos possíveis tipos de operação e requisição de hardware
class OPERATION_HW(Enum):
    """
    Enum for Operating Logs.
    """
    OP_HW_OK = 0xB1
    OP_HW_SLEEP = 0xB2
    OP_HW_BUSY = 0xB3
    OP_HW_FAIL = 0xB4

    @property
    def description(self):
        descriptions = {
            OPERATION_HW.OP_HW_OK: "System is awake",
            OPERATION_HW.OP_HW_SLEEP: "System is sleeping",
            OPERATION_HW.OP_HW_BUSY: "System is busy",
            OPERATION_HW.OP_HW_FAIL: "Hardware failed",
        }
        return descriptions.get(self, "Unknown status")


class REQUEST_HW(Enum):
    """
    Enum for Request Logs.
    """

    REQ_HW_NO_REQUEST = 0xB1
    REQ_HW_TX_TEMP = 0xB2
    REQ_HW_TX_CURRENTS = 0xB3
    REQ_HW_TX_POWERS = 0xB4
    REQ_HW_TX_TOT_PWR = 0xB5

    @property
    def description(self):
        descriptions = {
            REQUEST_HW.REQ_HW_NO_REQUEST: "No request has been made.",
            REQUEST_HW.REQ_HW_TX_TEMP: "Request Temperature",
            REQUEST_HW.REQ_HW_TX_CURRENTS: "Request Individual Currents",
            REQUEST_HW.REQ_HW_TX_POWERS: "Request Individual Power",
            REQUEST_HW.REQ_HW_TX_TOT_PWR: "Request Total Power"
        }
        return descriptions.get(self, "Unknown request")


class NASCERR_STATUS:
    """
    Class representing NASCERR Status
    """

    def __init__(self,
                 op_exp: OPERATION_EXP,
                 op_hw: OPERATION_HW,
                 req_exp: REQUEST_EXP,
                 req_hw: REQUEST_HW,
                 fail_flag: int):
        self.op_exp = op_exp
        self.op_hw = op_hw
        self.req_exp = req_exp
        self.req_hw = req_hw
        self.fail_flag = fail_flag

    def __repr__(self):
        return (
            f"NASCERR_STATUS(\n"
            f"  op_exp={self.op_exp.name},\n"
            f"  op_hw={self.op_hw.name},\n"
            f"  req_exp={self.req_exp.name},\n"
            f"  req_hw={self.req_hw.name},\n"
            f"  fail_flag={self.fail_flag}\n"
            f")"
        )

    def to_dict(self):
        current_names = [
            "current_1v2",
            "current_2v5",
            "current_2v5_vpp",
            "current_3v3",
            "current_3v3_FPGA",
        ]

        current_fail_bits = self.fail_flag & 0b0011111
        temperature_fail = (self.fail_flag >> 5) & 0b1
        queue_bits = (self.fail_flag >> 6) & 0b11

        queue_status_map = {
            0b00: "below_50_percent",
            0b01: "between_50_and_75_percent",
            0b10: "between_75_and_100_percent",
            0b11: "100_percent",
        }

        current_flags = {
            name: bool((current_fail_bits >> i) & 0b1)
            for i, name in enumerate(current_names)
        }

        active_current_failures = [
            name for name, failed in current_flags.items() if failed
        ]

        return {
            "operation": {
                "EXP": {
                    "name": self.op_exp.name,
                    "value": self.op_exp.value,
                    "description": self.op_exp.description,
                },
                "HW": {
                    "name": self.op_hw.name,
                    "value": self.op_hw.value,
                    "description": self.op_hw.description
                }
            },
            "request": {
                "EXP": {
                    "name": self.req_exp.name,
                    "value": self.req_exp.value,
                    "description": self.req_exp.description
                },
                "HW": {
                    "name": self.req_hw.name,
                    "value": self.req_hw.value,
                    "description": self.req_hw.description
                }
            },
            "fail_flag": {
                "raw": format(self.fail_flag, "08b"),
                "active_current_failures": active_current_failures,
                "temperature_fail": bool(temperature_fail),
                "queue_level_description": queue_status_map.get(queue_bits, "unknown")
            }
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)

    @classmethod
    def from_list(cls, lst):
        """
        Constroi uma instancia de NASCERR_STATUS a partir de uma lista de 5 valores:
        [op_hw, op_exp, req_exp, req_hw, fail_flag]

        @param lst: Lista contendo exatamente 5 inteiros.
        @return: Instância de NASCERR_STATUS.
        """
        if len(lst) != 5:
            raise ValueError(
                "the list must have 5 elements: [op_exp, req_exp, op_hw, req_hw, fail_flag]")

        op_exp_val, op_hw_val, req_exp_val, req_hw_val, fail_flag_val = lst

        try:
            op_exp = OPERATION_EXP(op_exp_val)
        except ValueError:
            raise ValueError(
                f"Invalid value for OPERATION_EXP: {op_exp_val}, data: {lst}")

        try:
            req_exp = REQUEST_EXP(req_exp_val)
        except ValueError:
            raise ValueError(f"Invalid value for REQUEST_EXP: {req_exp_val}")

        try:
            op_hw = OPERATION_HW(op_hw_val)
        except ValueError:
            raise ValueError(f"Invalid value for OPERATION_HW: {op_hw_val}")

        try:
            req_hw = REQUEST_HW(req_hw_val)
        except ValueError:
            raise ValueError(f"Invalid value for REQUEST_HW: {req_hw_val}")

        return cls(op_exp, op_hw, req_exp, req_hw, fail_flag_val)


class LOG_TYPE(Enum):
    """
    Enum for Type Logs.
    """
    LOG_VOID = 0x00
    LOG_SUCCESS = 0x01
    LOG_RECEIVE_COMMAND = 0x02
    LOG_RECEIVE_HW_STAT_REQ = 0x03
    LOG_TRANSMIT_HW_RESPONSE = 0x04
    LOG_TRANSMIT_HW_FINISH = 0x05
    LOG_HW_WARN = 0x06
    LOG_HW_ERROR = 0x07
    LOG_TRANSMIT_RESPONSE = 0x08
    LOG_TRANSMIT_FINISH = 0x09
    LOG_EXP_EXECUT = 0x0A
    LOG_EXP_FINNISH = 0x0B
    LOG_SYSTEM_BUSY = 0x0C
    LOG_FAIL = 0x0D
    LOG_MAX = 0x0E
    LOG_BOOT_INIRIALIZED = 0x0F
    LOG_BOOT_RECEIVE_COMMAND = 0x10
    LOG_BOOT_OPERATION = 0x11
    LOG_ROBUST_MEM_HEALTH = 0x12
    LOG_DB = 0x13
    LOG_EXP_RECEIVED = 0x14
    LOG_BOOT = 0x15
    LOG_CRC = 0x16

    @property
    def description(self):
        descriptions = {
            LOG_TYPE.LOG_VOID:                "Sem log",
            LOG_TYPE.LOG_SUCCESS:             "Operacao bem-sucedida.",
            LOG_TYPE.LOG_RECEIVE_COMMAND:     "Comando recebido e em processamento.",
            LOG_TYPE.LOG_RECEIVE_HW_STAT_REQ: "Comando de recebibo de algum aspecto do hardware.",
            LOG_TYPE.LOG_TRANSMIT_HW_RESPONSE: "Resposta de hardware pronto para ser transmitida.",
            LOG_TYPE.LOG_TRANSMIT_HW_FINISH:  "Comando de hardware transmitido com sucesso.",
            LOG_TYPE.LOG_HW_WARN:             "Alerta de hardware.",
            LOG_TYPE.LOG_HW_ERROR:            "Erro de hardware.",
            LOG_TYPE.LOG_TRANSMIT_RESPONSE:   "Resposta em transmissao",
            LOG_TYPE.LOG_TRANSMIT_FINISH:     "Transmissão finalizada com sucesso",
            LOG_TYPE.LOG_EXP_EXECUT:          "Experimento em execucao",
            LOG_TYPE.LOG_EXP_FINNISH:         "Experimento finalizado com sucesso",
            LOG_TYPE.LOG_SYSTEM_BUSY:         "Sistema ocupado, nao pode aceitar novo comando",
            LOG_TYPE.LOG_FAIL:                "Operacao falhou (erro generico)",
            LOG_TYPE.LOG_MAX:                 "Valor maximo",
            LOG_TYPE.LOG_BOOT_INIRIALIZED:    "Bootloader inicializado",
            LOG_TYPE.LOG_BOOT_RECEIVE_COMMAND: "Bootloader recebeu comando",
            LOG_TYPE.LOG_BOOT_OPERATION:      "Operacao do bootloader em andamento",
            LOG_TYPE.LOG_ROBUST_MEM_HEALTH:   "Operacao na memoria robusta em andamento",
            LOG_TYPE.LOG_DB:                  "Operacao de banco de dados (SRAM)",
            LOG_TYPE.LOG_EXP_RECEIVED:        "Experimento recebido e agendado",
            LOG_TYPE.LOG_BOOT:                "Operacao de bootloader",
            LOG_TYPE.LOG_CRC:                 "Operacao de CRC"
        }
        return descriptions.get(self, "Unknown request")


class ERROS_LOGS(Enum):
    """
    Enum for Error Logs.
    """
    ERR_VOID = 0x00
    ERR_NONE = 0x01
    WARN_COMM_DECODE = 0x02
    WARM_HW_STATUS_CODE = 0x03

    ERR_COMM_SENDPKT = 0x04
    ERR_EXP_INVALID_DATALEN = 0x05
    ERR_EXP_MEM_OVERFLOW = 0x06
    ERR_EXP_INVALID_REPETITIONS = 0x07
    ERR_EXP_INVALID_DELAY = 0x08
    ERR_EXP_CONFLICT = 0x09
    ERR_EXP_SCH_FULL = 0x0A
    ERR_EXP_READ_OUT_OF_RANGE = 0x0B
    ERR_I2C_INIT_FAILED = 0x0C
    ERR_I2C_BUSY = 0x0D
    ERR_I2C_TRANSFER = 0x0E
    ERR_I2C_TIMEOUT = 0x0F
    ERR_FLASH_OPERATION = 0x10
    ERR_FLASH_ERASE_FAILED = 0x11
    ERR_PROTO_CMD_DECODE = 0x12
    ERR_PROTO_SEND_PACKET = 0x13
    ERR_COMM_GENERAL = 0x14
    ERR_CMD_INVALID = 0x15
    ERR_CMD_OVERFLOW = 0x16
    ERR_CMD_CRC = 0x17
    ERR_DB_INIT = 0x18
    ERR_DB_WRITE = 0x19

    ERR_BOOT_CMD_SIZE = 0x1A
    ERR_BOOT_CMD_INIT = 0x1B
    ERR_BOOT_CMD_DECODE = 0x1C
    ERR_BOOT_FLASH_WRITE = 0x1D
    ERR_BOOT_FLASH_ERASE = 0x1E
    ERR_BOOT_FLASH_WRITE_SIZE = 0x1F
    ERR_BOOT_FLASH_CRC = 0x20

    ERR_DB_READ = 0x21

    ERR_BOOT_PKG_OUT_OF_SEQ = 0x22
    ERR_BOOT_PKG_INVALID_VER = 0x23
    ERR_BOOT_TIMEOUT = 0x24

    WARN_HW_CURRENT_THRESHOLD = 0x25

    ERR_BOOT_INVALID_METADATA = 0x26
    ERR_BOOT_SHA_MISMATCH = 0x27
    ERR_BOOT_UNRECOVERABLE = 0x28
    WARN_HW_TEMPERATURE_THRESHOLD = 0x29
    WARN_ROBUST_MEM_SRAM_1 = 0x2A
    WARN_ROBUST_MEM_SRAM_2 = 0x2B
    WARN_ROBUST_MEM_ENC_DEC = 0x2C
    ERR_ROBUST_MEM_ENC_DEC = 0x2D
    WARM_IWDG_RESET = 0x2E

    @property
    def description(self):
        descriptions = {
            ERROS_LOGS.ERR_VOID: "Sem erro",
            ERROS_LOGS.ERR_NONE: "Sem erro",
            ERROS_LOGS.WARN_COMM_DECODE: "Falha na decodificaco do comando",
            ERROS_LOGS.WARM_HW_STATUS_CODE: "Tentativa de acesso de status de hardware inexistente",
            ERROS_LOGS.ERR_COMM_SENDPKT: "Falha no envio do pacote",
            ERROS_LOGS.ERR_EXP_INVALID_DATALEN: "Experimento interrompido: dado de tamanho invalido",
            ERROS_LOGS.ERR_EXP_MEM_OVERFLOW: "Experimento interrompido: estouro de região de memoria",
            ERROS_LOGS.ERR_EXP_INVALID_REPETITIONS: "Experimento interrompido: numero de repeticoes invalido",
            ERROS_LOGS.ERR_EXP_INVALID_DELAY: "Experimento interrompido: valor de delay invalido",
            ERROS_LOGS.ERR_EXP_CONFLICT: "Experimento em conflito",
            ERROS_LOGS.ERR_EXP_SCH_FULL: "Agendador cheio",
            ERROS_LOGS.ERR_EXP_READ_OUT_OF_RANGE: "Tentativa de leitura fora do intervalo de experimentos",
            ERROS_LOGS.ERR_I2C_INIT_FAILED: "Erro na inicialização da comunicacao I2C",
            ERROS_LOGS.ERR_I2C_BUSY: "Comunicação I2C ocupada",
            ERROS_LOGS.ERR_I2C_TRANSFER: "Erro na transmissao/recepção I2C",
            ERROS_LOGS.ERR_I2C_TIMEOUT: "Transmissao finalizada por atingir timeout",
            ERROS_LOGS.ERR_FLASH_OPERATION: "Erro de operacao na flash",
            ERROS_LOGS.ERR_FLASH_ERASE_FAILED: "Erro ao apagar bloco na flash",
            ERROS_LOGS.ERR_PROTO_CMD_DECODE: "Falha na decodificacao do comando",
            ERROS_LOGS.ERR_PROTO_SEND_PACKET: "Falha no envio do pacote",
            ERROS_LOGS.ERR_COMM_GENERAL: "Erro de comunicacao generico",
            ERROS_LOGS.ERR_CMD_INVALID: "Comando invalido ou desconhecido ",
            ERROS_LOGS.ERR_CMD_OVERFLOW: "Estouro na fila de Comando",
            ERROS_LOGS.ERR_CMD_CRC: "Erro de CRC no comando",
            ERROS_LOGS.ERR_DB_INIT: "Erro ao inicializar o banco de dados",
            ERROS_LOGS.ERR_DB_WRITE: "Erro ao escrever no banco de dados",
            ERROS_LOGS.ERR_BOOT_CMD_SIZE: "Tamanho do comando do bootloader invalido",
            ERROS_LOGS.ERR_BOOT_CMD_INIT: "Erro ao iniciar o comando do bootloader",
            ERROS_LOGS.ERR_BOOT_CMD_DECODE: "Erro ao decodificar o comando do bootloader",
            ERROS_LOGS.ERR_BOOT_FLASH_WRITE: "Erro ao escrever na flash",
            ERROS_LOGS.ERR_BOOT_FLASH_ERASE: "Erro ao apagar a flash",
            ERROS_LOGS.ERR_BOOT_FLASH_WRITE_SIZE: "Tamanho da escrita na flash invalido",
            ERROS_LOGS.ERR_BOOT_FLASH_CRC: "Erro de CRC da flash",
            ERROS_LOGS.ERR_DB_READ: "Erro ao ler do banco de dados",
            ERROS_LOGS.ERR_BOOT_PKG_OUT_OF_SEQ: "Pacote fora de sequencia",
            ERROS_LOGS.ERR_BOOT_PKG_INVALID_VER: "Versao do pacote invalida",
            ERROS_LOGS.ERR_BOOT_TIMEOUT: "Timeout do bootloader",
            ERROS_LOGS.WARN_HW_CURRENT_THRESHOLD: "Alerta de corrente acima do limite",
            ERROS_LOGS.ERR_BOOT_INVALID_METADATA: "Metadados do firmware invalidos",
            ERROS_LOGS.ERR_BOOT_SHA_MISMATCH: "SHA-256 do firmware nao confere",
            ERROS_LOGS.ERR_BOOT_UNRECOVERABLE: "Erro critico no bootloader",
            ERROS_LOGS.WARN_HW_TEMPERATURE_THRESHOLD: "Alerta de temperatura acima do limite",
            ERROS_LOGS.WARN_ROBUST_MEM_SRAM_1: "Indica que houve corrupção de um novo bloco na SRAM 1",
            ERROS_LOGS.WARN_ROBUST_MEM_SRAM_2: "Indica que houve corrupção de um novo bloco na SRAM 2",
            ERROS_LOGS.WARN_ROBUST_MEM_ENC_DEC: "Indica que houve um erro nos encoders/decoders",
            ERROS_LOGS.ERR_ROBUST_MEM_ENC_DEC: "Indica que houve a saturação nos erros nos encoders/decoders",
            ERROS_LOGS.WARM_IWDG_RESET: "Indica que houve um reset pelo watchdog independente (IWDG)"
        }
        return descriptions.get(self, "Unknown error")


class NASCERR_MODE(Enum):
    """
    Enum for NASCERR Mode.
    """
    BYPASS_1 = 1
    BYPASS_2 = 2
    TBEC = 3
    MRSC = 4
    DUAL_BYPASS = 5

    @property
    def description(self):
        descriptions = {
            NASCERR_MODE.BYPASS_1: "Bypass operation to SRAM 1.",
            NASCERR_MODE.BYPASS_2: "Bypass operation to SRAM 2.",
            NASCERR_MODE.TBEC: "Error Correction Code TBEC_RSC in both SRAMs.",
            NASCERR_MODE.MRSC: "Error Correction Code MRSC in both SRAMs.",
            NASCERR_MODE.DUAL_BYPASS: "Bypass in SRAM1 and SRAM2"
        }
        return descriptions.get(self, "Unknown mode")

    def to_dict(self):
        return {
            "mode": {
                "name": self.name,
                "value": hex(self.value),
                "description": self.description
            }
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)


# class NASCERR_LOG:
#     """
#     Class representing NASCERR Logs.
#     """

#     def __init__(self, timestamp: int, type: LOG_TYPE, status: int, error_code: ERROS_LOGS):
#         self.timestamp = timestamp
#         self.type = type
#         self.status = status
#         self.error_code = error_code

#     def __repr__(self):
#         return (f"NASCERR_LOG(timestamp={self.timestamp}, "
#                 f"type={self.type.name}, "
#                 f"status={self.status}, "
#                 f"error_code={self.error_code.name})")

#     def to_dict(self):
#         return {
#             "timestamp": self.timestamp,
#             "type": {
#                 "name": self.type.name,
#                 "value": self.type.value,
#                 "description": self.type.description
#             },
#             "status": self.status,
#             "error_code": {
#                 "name": self.error_code.name,
#                 "value": self.error_code.value,
#                 "description": self.error_code.description
#             }
#         }

#     def to_json(self):
#         return json.dumps(self.to_dict(), indent=4)

#     @classmethod
#     def from_list(cls, lst):
#         """
#         Converte uma lista de 7 elementos em uma instância de NASCERR_LOG.
#         """
#         if len(lst) != 7:
#             raise ValueError(
#                 "A lista deve conter exatamente 7 elementos (timestamp, type, status, error_code).")

#         timestamp_value = int.from_bytes(lst[0:4], byteorder='little')
#         type_value = lst[4]
#         status_value = lst[5]
#         error_code_value = lst[6]

#         # Verifica se os valores são válidos
#         if type_value not in LOG_TYPE._value2member_map_:
#             raise ValueError(f"Valor invalido para LOG_TYPE: {type_value}")
#         if error_code_value not in ERROS_LOGS._value2member_map_:
#             raise ValueError(
#                 f"Valor invalido para ERROS_LOGS: {error_code_value}")

#         return cls(timestamp_value, LOG_TYPE(type_value), status_value, ERROS_LOGS(error_code_value))

#     @classmethod
#     def parse_multiple(cls, byte_list):
#         logs = []
#         for i in range(0, len(byte_list), 7):
#             chunk = byte_list[i:i+7]
#             if len(chunk) < 7:
#                 break  # Ignora pedaço incompleto
#             logs.append(cls.from_list(chunk))
#         return logs


class NASCERR_EXP_STATUS:
    """
    Class representing NASCERR experiment status telemetry.
    """
    _FORMAT = "<7H2B"  # little-endian: 7×uint16 (H), 2×uint8 (B)
    SIZE = struct.calcsize(_FORMAT)

    def __init__(self,
                 qtd_scheduler_total: int,
                 qtd_scheduler_pending: int,
                 qtd_scheduler_executed: int,
                 qtd_experiments_total: int,
                 qtd_experiments_running: int,
                 qtd_experiments_finished: int,
                 qtd_experiments_unread: int,
                 scheduler_full_flag: int,
                 flash_full_flag: int):
        self.qtd_scheduler_total = qtd_scheduler_total
        self.qtd_scheduler_pending = qtd_scheduler_pending
        self.qtd_scheduler_executed = qtd_scheduler_executed

        self.qtd_experiments_total = qtd_experiments_total
        self.qtd_experiments_running = qtd_experiments_running
        self.qtd_experiments_finished = qtd_experiments_finished
        self.qtd_experiments_unread = qtd_experiments_unread

        self.scheduler_full_flag = scheduler_full_flag
        self.flash_full_flag = flash_full_flag

    @classmethod
    def from_bytes(cls, data: bytes) -> "NASCERR_EXP_STATUS":
        """
        Unpack the first SIZE bytes of `data` into a new instance.
        """
        if len(data) < cls.SIZE:
            raise ValueError(
                f"Data too short: {len(data)} bytes (need {cls.SIZE})")
        values = struct.unpack(cls._FORMAT, data[:cls.SIZE])
        return cls(*values)

    def to_bytes(self) -> bytes:
        """
        Pack all fields into a byte buffer ready to send over I2C.
        """
        return struct.pack(
            self._FORMAT,
            self.qtd_scheduler_total,
            self.qtd_scheduler_pending,
            self.qtd_scheduler_executed,
            self.qtd_experiments_total,
            self.qtd_experiments_running,
            self.qtd_experiments_finished,
            self.qtd_experiments_unread,
            self.scheduler_full_flag,
            self.flash_full_flag
        )

    def to_dict(self) -> dict:
        return {
            "qtd_scheduler_total":    self.qtd_scheduler_total,
            "qtd_scheduler_pending":  self.qtd_scheduler_pending,
            "qtd_scheduler_executed": self.qtd_scheduler_executed,
            "qtd_experiments_total":    self.qtd_experiments_total,
            "qtd_experiments_running":  self.qtd_experiments_running,
            "qtd_experiments_finished": self.qtd_experiments_finished,
            "qtd_experiments_unread":   self.qtd_experiments_unread,
            "scheduler_full_flag": self.scheduler_full_flag,
            "flash_full_flag":     self.flash_full_flag
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=4)

    def __repr__(self) -> str:
        d = self.to_dict()
        fields = ", ".join(f"{k}={v}" for k, v in d.items())
        return f"NASCERR_EXP_STATUS({fields})"


class FlashRegionTypeDef(Enum):
    """
    Enum que espelha o typedef FlashRegionTypeDef do firmware.

    value → índice usado pelo firmware
    """
    FLASH_HEADER = 0
    FLASH_LOGS = 1
    FLASH_SCHEDULER = 2
    FLASH_EXPERIMENT = 3
    FLASH_BACKUP_FIRMWARE = 4
    MAX_FLASH_REGION = 5

    @property
    def description(self) -> str:
        desc = {
            FlashRegionTypeDef.FLASH_HEADER:          "Região de header/meta-dados",
            FlashRegionTypeDef.FLASH_LOGS:            "Área de logs de sistema",
            FlashRegionTypeDef.FLASH_SCHEDULER:       "Área de comandos agendados",
            FlashRegionTypeDef.FLASH_EXPERIMENT:      "Área de experimentos gravados",
            FlashRegionTypeDef.FLASH_BACKUP_FIRMWARE: "Backup de firmware/boot",
            FlashRegionTypeDef.MAX_FLASH_REGION:      "Valor sentinela (não usar)",
        }
        return desc.get(self, "Unknown region")

    # helpers opcionais
    def to_dict(self):
        return {
            "name":        self.name,
            "value":       self.value,
            "description": self.description,
        }

    def __repr__(self):
        return f"FlashRegionTypeDef.{self.name}({self.value})"


class NASCERR_ACK(IntEnum):
    """
    Enum for NASCERR ACK.
    """
    ACK_IDLE = 0
    ACK_WRITE_RECEIVED = 1
    ACK_CMD_PROCESSING = 2
    ACK_SUCESS = 3
    ACK_FAIL = 4

    @property
    def description(self):
        descriptions = {
            NASCERR_ACK.ACK_IDLE: "awaiting command",
            NASCERR_ACK.ACK_WRITE_RECEIVED: "write received",
            NASCERR_ACK.ACK_CMD_PROCESSING: "command processing",
            NASCERR_ACK.ACK_SUCESS: "success",
            NASCERR_ACK.ACK_FAIL: "fail"
        }
        return descriptions.get(self, "Unknown ACK")

    def to_dict(self):
        return {
            "ACK info": {
                "state": self.name,
                "value": self.value,
                "description": self.description
            }
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)


class NASCERR_IMAGE_STATUS:
    # uint8_t  valid;         /* 0xAA = imagem válida gravada      		*/
    # uint8_t  upload_state;  /* ImgUploadState_t                  		*/
    # uint8_t  progress_pct;  /* 0-100%                            		*/
    # uint32_t image_size;	  /* Total size of uploaded or uploading image*/
    # uint8_t  error_code;    /* ImgErrorCode_t                    		*/

    def __init__(self, valid: int, upload_state: int, progress_pct: int, image_size: int, error_code: int):
        self.valid = valid
        self.upload_state = upload_state
        self.progress_pct = progress_pct
        self.image_size = image_size
        self.error_code = error_code

    @property
    def description(self):
        valid_desc = "valid" if self.valid == 0xAA else "invalid"
        upload_state_desc = {
            0: "IMG_STATE_EMPTY",
            1: "IMG_STATE_UPLOADING",
            2: "IMG_STATE_VALIDATING",
            3: "IMG_STATE_READY",
            4: "IMG_STATE_ERROR"
        }.get(self.upload_state, "unknown")
        error_code_desc = {
            0: "IMG_ERR_NONE",
            1: "IMG_ERR_CRC_FRAME",
            2: "IMG_ERR_CRC_GLOBAL",
            3: "IMG_ERR_FLASH_WRITE",
            4: "IMG_ERR_SIZE",
            5: "IMG_ERR_BAD_FRAME",
            6: "IMG_ERR_TIMEOUT"
        }.get(self.error_code, "unknown")

        return (f"Image status: {valid_desc}, upload state: {upload_state_desc}, "
                f"progress: {self.progress_pct}%, size: {self.image_size} bytes, "
                f"error code: {error_code_desc}")

    def to_dict(self):
        return {
            "valid": self.valid,
            "upload_state": self.upload_state,
            "progress_pct": self.progress_pct,
            "image_size": self.image_size,
            "error_code": self.error_code,
            "description": self.description
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)

    @classmethod
    def from_bytes(cls, data: bytes) -> "NASCERR_IMAGE_STATUS":
        if len(data) < 8:
            raise ValueError(
                f"Data too short: {len(data)} bytes (need at least 8)")

        valid = data[0]
        upload_state = data[1]
        progress_pct = data[2]
        image_size = int.from_bytes(data[3:7], byteorder='little')
        error_code = data[7]

        return cls(valid, upload_state, progress_pct, image_size, error_code)
