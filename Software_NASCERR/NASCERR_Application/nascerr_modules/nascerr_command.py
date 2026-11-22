import enum
import json
import struct
from nascerr_modules.nascerr_system import NASCERR_MODE
from nascerr_modules.util.crc import calculate_crc16


class CmdTypeDef(enum.Enum):
    READ_CMD = 0
    WRITE_CMD = 1

class W_DataTypeDef(enum.Enum):
    WRITE_ZEROS = 0
    WRITE_ONES = 1
    WRITE_x55h = 2
    WRITE_xAAh = 3
    WRITE_IMAGE = 4

class Seq_ReadTypeDef(enum.Enum): # TODO: DELETAR
    # WORD_BY_WORD = 0
    BLOCK_BY_BLOCK = 1

class NASCERR_CMD:
    def __init__(self, cmd_type, id, block, qtd_block, ecc, write_type,
                 scheduler_time, repeat, delay, current_tick):
        self.cmd_type = CmdTypeDef(cmd_type)
        self.id = id
        self.block = block
        self.qtd_block = qtd_block
        self.ecc = NASCERR_MODE(ecc)
        self.write_type = W_DataTypeDef(write_type)
        self.scheduler_time = scheduler_time
        self.repeat = repeat
        self.delay = delay
        self.current_tick = current_tick

    def to_bytes(self):
        """
        Serialize the NASCERR_CMD structure to a bytes object and calculate
        bytes_msg and crc16.
        """
        # Temporarily create the message without the CRC to calculate the length
        message = struct.pack(
            '<B I H H B B I H I I',
            self.cmd_type.value,
            self.id,
            self.block,
            self.qtd_block,
            self.ecc.value,
            self.write_type.value,
            self.scheduler_time,
            self.repeat,
            self.delay,
            self.current_tick
        )

        # 2 bytes for bytes_msg, 2 bytes for crc16
        bytes_msg = len(message) + 2 + 2

        # Recreate the message with the correct bytes_msg value
        message = struct.pack(
            '>H B I H H B B I H I I',
            bytes_msg,
            self.cmd_type.value,
            self.id,
            self.block,
            self.qtd_block,
            self.ecc.value,
            self.write_type.value,
            self.scheduler_time,
            self.repeat,
            self.delay,
            self.current_tick
        )

        # Calculate CRC16
        crc16 = calculate_crc16(message)

        # Append the CRC16 to the message
        message += struct.pack('>H', crc16)

        return {
            "message_bytes": message,
            "bytes_msg": bytes_msg,
            "crc16": crc16,
        }

    def to_dict(self):
        """
        Convert the NASCERR_CMD configuration to a dictionary including
        serialized message details.
        """
        s = self.to_bytes()
        return {
            "bytes_msg": s["bytes_msg"],
            "id": self.id,
            "cmd_type": self.cmd_type.name,
            "block": self.block,
            "qtd_block": self.qtd_block,
            "ecc": self.ecc.name,
            "write_type": self.write_type.name,
            "scheduler_time": self.scheduler_time,
            "repeat": self.repeat,
            "delay": self.delay,
            "current_tick": self.current_tick,
            "crc16": f"{s['crc16']:04X}",
        }

    def to_json(self):
        """
        Convert the NASCERR_CMD configuration to a JSON string.
        """
        return json.dumps(self.to_dict(), indent=4)