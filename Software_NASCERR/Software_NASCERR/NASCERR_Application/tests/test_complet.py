"""
Full NASCERR communication test with fault injection.

Runs the hardware, logs and experiments flow. Every packet sent to a command
register is preceded by faulty versions of it:

  - crc           : CRC16 with inverted bits
  - data_byte     : one data byte corrupted, original CRC
  - data_bit      : a single inverted bit (data or CRC)
  - field_*       : valid CRC, but a field out of range (where applicable)

All variants have the same size as the original packet.

For each injected fault:
  - hung       the device stopped answering get_status
  - corrupted  the faulty packet had an observable effect on the state
  - detected   not hung and no effect

After the faults, the correct packet is sent and the flow continues normally.
The bootloader is not covered here (test_bootloader.py has its own injection).
"""

import json
import random
import struct
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List

import pytest

import metrics
from nascerr_modules.nascerr_command import CmdTypeDef, NASCERR_CMD, W_DataTypeDef
from nascerr_modules.nascerr_operation import Operation
from nascerr_modules.nascerr_registers import registers
from nascerr_modules.nascerr_system import (
    FlashRegionTypeDef, NASCERR_ACK, NASCERR_MODE, REQUEST_EXP,
)
from nascerr_modules.util.crc import calculate_crc16


# --- Configuration ---

DEVICE_NAME = "NASCERR_CargaUtil"
I2C_ADDRESS = 0x55

# Wait times (s)
WAIT_TIME = 0.1
MEASUREMENT_WAIT = 0.2
SLEEP_WAIT = 1.0
COMMAND_WAIT = 0.5

INTERNAL_FLASH_CAPACITY = 301
EXTERNAL_PAGE_CAPACITY = 301
TOTAL_EXTERNAL_PAGES = 64

# Temperature in °C, current in mA, power in mW
TEMPERATURE_LIMITS = {
    "min": -40.0, "max": 85.0,
    "nominal_min": 0.0, "nominal_max": 60.0
}
CURRENT_LIMITS = {
    "min": 0.0, "max": 5000.0,
    "nominal_min": 0.0, "nominal_max": 1000.0
}
POWER_LIMITS = {
    "min": 0.0, "max": 25000.0,
    "nominal_min": 0.0, "nominal_max": 5000.0
}

EXPECTED_CURRENT_CHANNELS = 5
EXPECTED_POWER_CHANNELS = 5

EXP_CMD_COUNT = 10
EXP_SCHEDULER_TIME = 180000   # ms; the sends with injection take ~2 min
EXP_READ_COUNT = 6

FAULTS = ("crc", "data_byte", "data_bit")
LOG_SAMPLING = 10             # in the log tests, inject on 1 out of every N packets
EFFECT_WAIT = 1.5             # s watching the state after each fault
EFFECT_POLL = 0.15
ACK_TIMEOUT = 5.0


# --- Communication fault injector ---

def _with_crc(payload: bytes) -> bytes:
    return payload + struct.pack(">H", calculate_crc16(payload))


class CommFaultInjector:
    """
    Intercepts Protocol.set_register. For each packet sent to a monitored
    register, first sends the faulty versions (raw I2C write, bypassing the
    Protocol checks), evaluates the outcome and only then sends the correct packet.
    """

    REGISTERS = {
        "HW_STAT":      "hw_stat",
        "SLEEP":        "sleep",
        "WAKEUP":       "wakeup",
        "CMD":          None,          # cmd_write / cmd_read by command type
        "EXP_SUM_IDX":  "exp_sum_idx",
        "LOG_SET_PAGE": "log_page",
    }

    def __init__(self, operation: Operation):
        self.op = operation
        self.proto = operation.communication
        self._original_set_register = self.proto.set_register
        self.proto.set_register = self._set_register
        self.active = True
        self.sampling = 1
        self._count = Counter()
        self._inside = False
        self.summary = Counter()   # (category, fault, result) -> n

    @contextmanager
    def paused(self):
        previous = self.active
        self.active = False
        try:
            yield
        finally:
            self.active = previous

    # --- Classification and observable state ---

    def _category(self, register: str, data: bytes):
        if register == "CMD":
            if len(data) < 3:
                return None
            return "cmd_write" if data[2] == CmdTypeDef.WRITE_CMD.value else "cmd_read"
        return self.REGISTERS.get(register)

    def _read_state(self, category: str):
        """State affected by the command, or None if there is no observable effect."""
        for _ in range(3):
            try:
                if category in ("sleep", "wakeup"):
                    r = self.op.get_status()
                    if not r.get("error"):
                        return r["data"]["operation"]["HW"]["value"]
                elif category == "cmd_read":
                    r = self.op.get_status()
                    if not r.get("error"):
                        return r["data"]["request"]["EXP"]["value"]
                elif category == "cmd_write":
                    r = self.op.get_experiment_status()
                    if not r.get("error"):
                        return r["data"]["qtd_scheduler_total"]
                elif category == "exp_sum_idx":
                    r = self.op.get_exp_sum()
                    if not r.get("error"):
                        return r["data"]["offset"]
                elif category == "log_page":
                    r = self.op.get_log_summary()
                    if not r.get("error"):
                        return r["data"]["selected_page"]
                else:
                    return None   # hw_stat has no observable effect
            except Exception:
                pass
            time.sleep(0.2)
        return "unreadable"

    def _responds(self) -> bool:
        for _ in range(3):
            try:
                r = self.op.get_status()
                if r and not r.get("error"):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    # --- Faulty packet construction ---

    def _changed_base(self, category: str, data: bytes) -> bytes:
        """
        Valid packet with a value different from the current one, so that a
        faulty packet accepted by mistake has an observable effect.
        """
        if category == "exp_sum_idx":
            current = self._read_state(category)
            new = 60 if current == 30 else 30
            return _with_crc(struct.pack("<B", new))
        if category == "log_page":
            current = self._read_state(category)
            new = 1 if current == 0 else 0
            return _with_crc(struct.pack("<B", new))
        return data

    def _cmd_field(self, data: bytes, field: str) -> bytes:
        """Rebuilds a WRITE_CMD with a valid CRC, an invalid field and a different id."""
        fmt = ">H B I H H B B I H I I"
        fields = list(struct.unpack(fmt, data[:struct.calcsize(fmt)]))
        fields[2] = (fields[2] + 0x800000) & 0xFFFFFFFF    # id that does not collide with the real one
        if field == "block_out_of_range":
            fields[3] = 70                                  # block > 63
        elif field == "qtd_zero":
            fields[4] = 0                                   # qtd_block = 0
        return _with_crc(struct.pack(fmt, *fields))

    @staticmethod
    def _corrupt(packet: bytes, idx: int, mask: int) -> bytes:
        b = bytearray(packet)
        b[idx] ^= mask
        return bytes(b)

    def _variants(self, category: str, data: bytes):
        """Every variant has exactly the size of the original packet."""
        base = self._changed_base(category, data)
        n_payload = len(base) - 2
        v = [
            ("crc", self._corrupt(base, len(base) - 1, 0xFF)),
            ("data_byte", self._corrupt(base, random.randrange(n_payload),
                                        random.randint(1, 0xFF))),
            ("data_bit", self._corrupt(base, random.randrange(len(base)),
                                       1 << random.randrange(8))),
        ]
        if category == "cmd_write":
            v.append(("field_block_out_of_range", self._cmd_field(data, "block_out_of_range")))
            v.append(("field_qtd_zero", self._cmd_field(data, "qtd_zero")))
        elif category == "hw_stat":
            v.append(("field_invalid", _with_crc(b"\xff")))
        elif category == "exp_sum_idx":
            v.append(("field_invalid", _with_crc(struct.pack("<B", 15))))  # not a multiple of 30
        for fault, bad in v:
            assert len(bad) == len(data), f"{category}/{fault}: {len(bad)} != {len(data)} bytes"
        return v

    # --- Interception ---

    def _set_register(self, register, data):
        category = self._category(register, bytes(data))
        if self._inside or not self.active or category is None:
            return self._original_set_register(register, data)

        self._count[category] += 1
        if self._count[category] % self.sampling != 0:
            return self._original_set_register(register, data)

        self._inside = True
        try:
            reg = registers.get(register)
            for fault, bad in self._variants(category, bytes(data)):
                self._inject(category, fault, reg, bad)
        finally:
            self._inside = False
        return self._original_set_register(register, data)

    def _stable_state(self, category, maximum: float = 3.0):
        """
        Reads the state until two consecutive reads match. The scheduler
        counter, for instance, updates with a delay after the previous command.
        """
        previous = self._read_state(category)
        if previous is None:
            return None
        end = time.time() + maximum
        while time.time() < end:
            time.sleep(0.4)
            current = self._read_state(category)
            if current == previous:
                return current
            previous = current
        return previous

    def _inject(self, category, fault, reg, bad):
        before = self._stable_state(category)
        sent = self.proto.i2c.write_data(reg.addr, list(bad))

        changed = False
        end = time.time() + EFFECT_WAIT
        while time.time() < end:
            time.sleep(EFFECT_POLL)
            if before is None:
                continue
            now = self._read_state(category)
            if "unreadable" not in (before, now) and now != before:
                changed = True
                break

        hung = not self._responds()
        if hung:
            time.sleep(5.0)   # give it a chance to recover before moving on

        detected = not hung and not changed
        if hung:
            result = "hung"
        elif changed:
            result = "corrupted"
        else:
            result = "detected"
        self.summary[(category, fault, result)] += 1

        detail = f"{len(bad)} bytes"
        if sent.get("error"):
            detail += " | rejected on the bus"
        if before is None:
            detail += " | no observable effect (responsiveness only)"
        elif before == "unreadable":
            detail += " | state unreadable before the fault"
        metrics.fault(f"comm_{category}_{fault}", detected, hung, detail)

    def print_summary(self):
        print("\n" + "=" * 80)
        print("COMMUNICATION FAULT INJECTION SUMMARY")
        print("=" * 80)
        keys = sorted({(c, f) for c, f, _ in self.summary})
        print(f"{'category':12s} {'fault':24s} {'detect.':>8s} {'corrupt.':>9s} {'hung':>7s}")
        for c, f in keys:
            print(f"{c:12s} {f:24s} {self.summary[(c, f, 'detected')]:8d} "
                  f"{self.summary[(c, f, 'corrupted')]:9d} {self.summary[(c, f, 'hung')]:7d}")


# --- Experiment helpers ---

class Emulated_Flash:
    """Expected state of the scheduled experiments."""

    list_write_cmd_exp = []
    summary = []

    @classmethod
    def reset(cls):
        cls.list_write_cmd_exp = []
        cls.summary = []

    @classmethod
    def expected_pages(cls):
        return sum(cmd.repeat + 1 for cmd in cls.list_write_cmd_exp)

    @classmethod
    def count_appearances_in_summary(cls, cmd):
        count = 0
        for summary_page in cls.summary:
            for entry in summary_page.get("data", {}).get("entries", []):
                if entry["id"] == cmd.id:
                    count += 1
        return count

    @classmethod
    def handle_summary(cls):
        return all(cls.count_appearances_in_summary(cmd) == cmd.repeat + 1
                   for cmd in cls.list_write_cmd_exp)


def _wait_ack(operation: Operation):
    end = time.time() + ACK_TIMEOUT
    while time.time() < end:
        r = operation.get_ack()
        if r and not r.get("error") and r.get("data"):
            value = r["data"]["ACK info"]["value"]
            if value in (NASCERR_ACK.ACK_SUCESS, NASCERR_ACK.ACK_FAIL):
                return NASCERR_ACK(value)
        time.sleep(0.05)
    return None


def _exp_status(operation: Operation, attempts: int = 5):
    for _ in range(attempts):
        r = operation.get_experiment_status()
        if r and not r.get("error") and r.get("data"):
            return r["data"]
        time.sleep(0.3)
    return None


# --- Tests ---

class TestNASCERRComplete:
    """Hardware, logs and experiments flow with communication fault injection."""

    @pytest.fixture(scope="class")
    def operation(self):
        print("\n" + "=" * 80)
        print("STARTING COMMUNICATION TEST WITH FAULT INJECTION")
        print("=" * 80)
        op = Operation(DEVICE_NAME, I2C_ADDRESS)

        status = op.get_status()
        assert not status["error"], "Failed to connect"
        print(f"✓ Device connected: {DEVICE_NAME}")

        op.injector = CommFaultInjector(op)
        yield op
        op.injector.print_summary()

    @pytest.fixture(scope="function")
    def ensure_awake(self, operation):
        """Makes sure the device is awake (without injection)."""
        with operation.injector.paused():
            try:
                operation.system_wakeup()
                time.sleep(WAIT_TIME)
            except Exception:
                pass
        yield
        with operation.injector.paused():
            try:
                operation.system_wakeup()
                time.sleep(WAIT_TIME)
            except Exception:
                pass

    @pytest.fixture(scope="class")
    def clean_device(self, operation):
        """Clears the logs and experiments areas."""
        print("\n" + "=" * 80)
        print("SETUP: Clearing logs and experiments areas")
        print("=" * 80)

        for name, region in (("logs", FlashRegionTypeDef.FLASH_LOGS),
                             ("experiments", FlashRegionTypeDef.FLASH_EXPERIMENT),
                             ("scheduler", FlashRegionTypeDef.FLASH_SCHEDULER)):
            r = operation.clear_database(region)
            assert not r["error"], f"Error clearing {name}: {r.get('log_error')}"
            print(f"✓ {name} cleared")
            time.sleep(WAIT_TIME)

        Emulated_Flash.reset()
        return True

    # --- Part 1: hardware ---

    @pytest.mark.hardware
    @pytest.mark.order(1)
    def test_01_hardware_temperature(self, operation, ensure_awake):
        """Temperature reading."""
        print("\n" + "=" * 80)
        print("TEST 1: Hardware - Temperature")
        print("=" * 80)

        result = operation.get_hardware_temperature()
        assert not result["error"], f"Error reading temperature: {result.get('log_error')}"

        temps = result["data"]
        assert len(temps) > 0, "No temperature returned"

        for i, temp in enumerate(temps):
            print(f"  Sensor {i}: {temp:.2f}°C")
            assert TEMPERATURE_LIMITS["min"] <= temp <= TEMPERATURE_LIMITS["max"]

        print("✓ Temperature test done")

    @pytest.mark.hardware
    @pytest.mark.order(2)
    def test_02_hardware_current_and_power(self, operation, ensure_awake):
        """Current and power reading."""
        print("\n" + "=" * 80)
        print("TEST 2: Hardware - Current and Power")
        print("=" * 80)

        current_result = operation.get_hardware_current()
        assert not current_result["error"], "Error reading current"
        currents = current_result["data"]
        assert len(currents) == EXPECTED_CURRENT_CHANNELS
        print(f"✓ Currents: {[f'{c:.2f}mA' for c in currents]}")

        time.sleep(MEASUREMENT_WAIT)

        power_result = operation.get_hardware_power()
        assert not power_result["error"], "Error reading power"
        powers = power_result["data"]
        assert len(powers) == EXPECTED_POWER_CHANNELS
        print(f"✓ Powers: {[f'{p:.2f}mW' for p in powers]}")

        print("✓ Current and power test done")

    @pytest.mark.hardware
    @pytest.mark.order(3)
    def test_03_hardware_sleep_wakeup(self, operation):
        """Sleep/wakeup cycles."""
        print("\n" + "=" * 80)
        print("TEST 3: Hardware - Sleep/Wakeup")
        print("=" * 80)

        num_cycles = 3
        for cycle in range(num_cycles):
            print(f"  Cycle {cycle + 1}/{num_cycles}...")
            operation.system_sleep()
            time.sleep(SLEEP_WAIT)
            operation.system_wakeup()
            time.sleep(SLEEP_WAIT)
            status = operation.get_status()
            assert not status["error"], f"Error in cycle {cycle + 1}"

        print(f"✓ {num_cycles} cycles completed")

    # --- Part 2: logs ---

    def generate_test_command(self, cmd_id: int, block: int = 0) -> Dict:
        return {
            "cmd_type": CmdTypeDef.WRITE_CMD,
            "id": cmd_id,
            "block": block,
            "qtd_block": 1,
            "ecc": NASCERR_MODE.MRSC,
            "write_type": W_DataTypeDef.WRITE_xAAh,
            "scheduler_time": 0,
            "repeat": 1,
            "delay": 0,
            "current_tick": 0
        }

    def get_log_summary(self, operation: Operation, retry: int = 3) -> Dict:
        for attempt in range(retry):
            try:
                result = operation.get_log_summary()
                time.sleep(WAIT_TIME)
                if not result.get("error"):
                    return result
                time.sleep(WAIT_TIME * 2)
            except Exception:
                time.sleep(WAIT_TIME * 2)
        return {"error": True, "log_error": "Failed after multiple attempts"}

    def set_and_read_log_page(self, operation: Operation, page: int, retry: int = 3) -> List[Dict]:
        set_result = operation.set_log_page(page)
        assert not set_result["error"], f"Error setting page {page}"
        time.sleep(WAIT_TIME * 2)

        for attempt in range(retry):
            read_result = operation.get_log_page_content()
            if not read_result["error"]:
                time.sleep(WAIT_TIME)
                return read_result.get("data", [])
            time.sleep(WAIT_TIME * 3)

        raise AssertionError(f"Error reading page {page}")

    @pytest.mark.order(4)
    def test_04_logs_fill_internal_flash(self, operation, clean_device):
        """Fills the internal flash."""
        print("\n" + "=" * 80)
        print(f"TEST 4: Logs - Fill Internal Flash ({INTERNAL_FLASH_CAPACITY} logs)")
        print("=" * 80)

        operation.injector.sampling = LOG_SAMPLING
        try:
            logs_sent = 0
            for i in range(INTERNAL_FLASH_CAPACITY):
                cmd_params = self.generate_test_command(cmd_id=i + 1, block=i % 192)
                result = operation.send_commandNew(**cmd_params)
                if not result["error"]:
                    logs_sent += 1
                time.sleep(COMMAND_WAIT)

                if (i + 1) % 50 == 0:
                    print(f"  Progress: {i + 1}/{INTERNAL_FLASH_CAPACITY}")
        finally:
            operation.injector.sampling = 1

        print(f"✓ {logs_sent}/{INTERNAL_FLASH_CAPACITY} logs sent")

        time.sleep(5.0)
        summary = self.get_log_summary(operation, retry=5)
        assert not summary["error"], "Error getting summary"
        print(f"✓ Pages created: {summary['data']['total_pages']}")

    @pytest.mark.order(5)
    def test_05_logs_verify_pages(self, operation, clean_device):
        """Checks the logs in flash."""
        print("\n" + "=" * 80)
        print("TEST 5: Logs - Verify Pages")
        print("=" * 80)

        logs_page_0 = self.set_and_read_log_page(operation, 0)
        print(f"✓ Page 0: {len(logs_page_0)} logs")
        assert len(logs_page_0) > 0, "No logs on page 0"

    @pytest.mark.order(6)
    def test_06_logs_overflow_external(self, operation, clean_device):
        """Overflow into external memory."""
        print("\n" + "=" * 80)
        print("TEST 6: Logs - Overflow to External")
        print("=" * 80)

        extra_logs = EXTERNAL_PAGE_CAPACITY * 3
        logs_sent = 0
        start_id = INTERNAL_FLASH_CAPACITY + 1

        operation.injector.sampling = LOG_SAMPLING
        try:
            for i in range(extra_logs):
                cmd_id = start_id + i
                cmd_params = self.generate_test_command(cmd_id=cmd_id, block=i % 192)
                result = operation.send_commandNew(**cmd_params)
                if not result["error"]:
                    logs_sent += 1
                time.sleep(COMMAND_WAIT)

                if (i + 1) % 100 == 0:
                    print(f"  Progress: {i + 1}/{extra_logs}")
        finally:
            operation.injector.sampling = 1

        print(f"✓ {logs_sent} additional logs sent")

        summary = self.get_log_summary(operation)
        assert not summary["error"], "Error getting summary"
        total_pages = summary["data"]["total_pages"]
        print(f"✓ Total pages: {total_pages}")
        assert total_pages >= 4, f"Expected >= 4 pages, got {total_pages}"

    # --- Part 3: experiments ---

    @pytest.mark.order(7)
    def test_07_experiments_write(self, operation, clean_device):
        """Schedules experiments without block conflicts and checks the scheduler."""
        print("\n" + "=" * 80)
        print("TEST 7: Experiments - Write")
        print("=" * 80)

        # The log tests also schedule commands: clear before starting
        with operation.injector.paused():
            for region in (FlashRegionTypeDef.FLASH_SCHEDULER, FlashRegionTypeDef.FLASH_EXPERIMENT):
                r = operation.clear_database(region)
                assert not r["error"], f"Error clearing {region}"
                time.sleep(10)
        Emulated_Flash.reset()

        ecc = random.choice([NASCERR_MODE.MRSC, NASCERR_MODE.TBEC])
        write_type = random.choice([
            W_DataTypeDef.WRITE_x55h, W_DataTypeDef.WRITE_ONES,
            W_DataTypeDef.WRITE_ZEROS, W_DataTypeDef.WRITE_xAAh,
        ])

        # Only conflict-free commands: all of them must be accepted
        free = set(range(64))
        cmd_list = []
        while free and len(cmd_list) < EXP_CMD_COUNT:
            blk = random.choice(sorted(free))
            qtd = 1
            while qtd < 3 and (blk + qtd) in free:
                qtd += 1
            qtd = random.randint(1, qtd)
            free -= set(range(blk, blk + qtd))
            cmd_list.append(NASCERR_CMD(
                cmd_type=CmdTypeDef.WRITE_CMD, id=len(cmd_list) + 1,
                block=blk, qtd_block=qtd, ecc=ecc, write_type=write_type,
                scheduler_time=EXP_SCHEDULER_TIME, repeat=random.randint(0, 2),
                delay=EXP_SCHEDULER_TIME // 4, current_tick=0,
            ))

        rejected = []
        for cmd in cmd_list:
            result = operation.send_commandNew(
                cmd_type=cmd.cmd_type, id=cmd.id, block=cmd.block,
                qtd_block=cmd.qtd_block, ecc=cmd.ecc, write_type=cmd.write_type,
                scheduler_time=cmd.scheduler_time, repeat=cmd.repeat,
                delay=cmd.delay, current_tick=cmd.current_tick,
            )
            assert result is not None and result["error"] == False
            ack = _wait_ack(operation)
            if ack == NASCERR_ACK.ACK_SUCESS:
                Emulated_Flash.list_write_cmd_exp.append(cmd)
            else:
                rejected.append((cmd.id, ack.name if ack else "no ACK"))
            time.sleep(0.5)

        print(f"✓ {len(cmd_list)} commands sent, {len(Emulated_Flash.list_write_cmd_exp)} accepted")

        # The scheduler counter updates with a delay
        expected = len(Emulated_Flash.list_write_cmd_exp)
        st = None
        for _ in range(10):
            st = _exp_status(operation)
            if st and st["qtd_scheduler_total"] >= expected:
                break
            time.sleep(0.5)
        assert st is not None, "Could not read EXP_STAT"
        print(f"✓ Commands in scheduler: {st['qtd_scheduler_total']}")
        assert not rejected, f"Conflict-free commands rejected: {rejected}"
        assert st["qtd_scheduler_total"] == expected

    @pytest.mark.order(8)
    def test_08_experiments_summary(self, operation, clean_device):
        """Waits for the executions and checks the summary."""
        print("\n" + "=" * 80)
        print("TEST 8: Experiments - Summary")
        print("=" * 80)

        expected_pages = Emulated_Flash.expected_pages()
        print(f"Expected pages: {expected_pages}")

        # scheduler_time (3 min) + up to 2 repeats every scheduler_time/4
        max_wait = EXP_SCHEDULER_TIME / 1000 * 1.5 + 90
        start = time.time()
        unread = 0
        while time.time() - start < max_wait:
            st = _exp_status(operation, attempts=1)
            if st:
                unread = st["qtd_experiments_unread"]
                if unread >= expected_pages:
                    break
            time.sleep(5)
        print(f"  {unread}/{expected_pages} pages ready")
        assert unread >= expected_pages, "Not all executions finished in time"

        offset = 0
        qtd_sum_pages = (expected_pages + 29) // 30
        for i in range(qtd_sum_pages):
            print(f"  Reading summary {i + 1}/{qtd_sum_pages} (offset={offset})")
            operation.set_exp_sum_idx(offset)
            time.sleep(1)
            summary = operation.get_exp_sum()
            assert summary is not None and summary["error"] == False
            Emulated_Flash.summary.append(summary)
            offset += 30
            time.sleep(2)

        validation = Emulated_Flash.handle_summary()
        if not validation:
            for cmd in Emulated_Flash.list_write_cmd_exp:
                print(f"  Cmd ID {cmd.id}: expected {cmd.repeat + 1}, "
                      f"found {Emulated_Flash.count_appearances_in_summary(cmd)}")
        assert validation == True
        print("✓ Summary verified")

    @pytest.mark.order(9)
    def test_09_experiments_read(self, operation, clean_device):
        """Reads the first experiments of the summary, in page order."""
        print("\n" + "=" * 80)
        print("TEST 9: Experiments - Read")
        print("=" * 80)

        entries = [e for pag in Emulated_Flash.summary
                   for e in pag.get("data", {}).get("entries", [])]
        assert entries, "Empty summary"

        for page, entry in enumerate(entries[:EXP_READ_COUNT]):
            result = operation.send_commandNew(
                cmd_type=CmdTypeDef.READ_CMD, id=entry["id"], block=page,
                qtd_block=entry["num_block"], ecc=NASCERR_MODE.MRSC,
                write_type=0, scheduler_time=0, repeat=0, delay=0, current_tick=0,
            )
            assert result["error"] == False
            time.sleep(0.5)

            # Read progress is reported through request.EXP, not the ACK
            ready = False
            end = time.time() + 30
            time.sleep(1)
            while time.time() < end:
                st = operation.get_status()
                if st and not st.get("error") and st.get("data"):
                    req = st["data"]["request"]["EXP"]["value"]
                    if req == REQUEST_EXP.REQ_EXP_TX_RESULT.value:
                        ready = True
                        break
                    if req == REQUEST_EXP.REQ_EXP_FAIL.value:
                        break
                time.sleep(1)
            assert ready, f"Read of page {page} (exp {entry['id']}) never became ready"

            read_result = operation.read_experiment()
            assert read_result["error"] == False and read_result["data"] is not None
            print(f"  ✓ Page {page}: exp {entry['id']} read")
            time.sleep(2)

        print(f"✓ {min(len(entries), EXP_READ_COUNT)} experiments read")

    # --- Part 4: final diagnostic ---

    @pytest.mark.order(10)
    def test_10_final_diagnostic(self, operation, ensure_awake):
        """Checks that the system still responds after all the faults."""
        print("\n" + "=" * 80)
        print("TEST 10: Final System Diagnostic")
        print("=" * 80)

        with operation.injector.paused():
            status = operation.get_status()
            temp = operation.get_hardware_temperature()
            current = operation.get_hardware_total_current()
            power = operation.get_hardware_total_power()

        diagnostic = {
            "status": not status["error"],
            "temperature": None if temp["error"] else temp["data"][0],
            "current": None if current["error"] else current["data"][0],
            "power": None if power["error"] else power["data"][0],
        }
        for k, v in diagnostic.items():
            print(f"{'✓' if v not in (None, False) else '✗'} {k}: {v}")

        report_path = Path("test_results") / f"diagnostic_{time.strftime('%Y%m%d_%H%M%S')}.json"
        report_path.parent.mkdir(exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(diagnostic, f, indent=2, default=str)
        print(f"✓ Report saved: {report_path}")

        assert all(v not in (None, False) for v in diagnostic.values()), \
            "System does not respond normally after fault injection"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])
