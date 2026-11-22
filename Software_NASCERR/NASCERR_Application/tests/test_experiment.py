"""
Experiment flow tests: schedule WRITE commands, wait for execution, check the
experiment summary and read every experiment back verifying ECC telemetry.

Emulated_Flash mirrors the firmware scheduler rules so each command's outcome
can be predicted. Results are recorded through the `metrics` module.
"""

from random import randint, choice
from time import sleep, monotonic

from nascerr_modules.nascerr_command import *
from nascerr_modules.nascerr_operation import Operation
from nascerr_modules.nascerr_system import FlashRegionTypeDef, NASCERR_ACK, REQUEST_EXP

import metrics

ACK_TIMEOUT = 5.0  # s
ACK_POLL = 0.1     # s

FLASH_SCHEDULER = 2
FLASH_EXPERIMENT = 3

TEST_QUEUE = "experiment_queue"          # each packet sent = 1 request
TEST_EXECUTION = "experiment_execution"  # each scheduled experiment = 1 request

ECC_ZERO_FIELDS = ("single_bit_errors", "double_bit_errors", "triple_bit_errors",
                   "beg_region_errors", "mid_region_errors", "end_region_errors",
                   "uncorrectable_errors", "ecc_status")


# --- Helpers ---

def _wait_ack(operation: Operation):
    """Wait for the ACK to reach SUCESS or FAIL. Returns the final state or None on timeout."""
    end = monotonic() + ACK_TIMEOUT
    while monotonic() < end:
        r = operation.get_ack()
        if r and not r.get("error") and r.get("data"):
            value = r["data"]["ACK info"]["value"]
            if value in (NASCERR_ACK.ACK_SUCESS, NASCERR_ACK.ACK_FAIL):
                return NASCERR_ACK(value)
        sleep(ACK_POLL)
    return None


def _wait_scheduled_count(operation: Operation, expected: int, timeout: float = 2.0):
    """
    qtd_scheduler_total is updated after the ACK. Wait until it reaches
    `expected` or the timeout expires. Returns the last value read (or None).
    """
    end = monotonic() + timeout
    last = None
    while True:
        value = _scheduled_count(operation)
        if value is not None:
            last = value
            if value >= expected:
                return value
        if monotonic() >= end:
            return last
        sleep(ACK_POLL)


def _scheduled_count(operation: Operation, attempts: int = 3):
    """qtd_scheduler_total from EXP_STAT, or None if it could not be read."""
    for _ in range(attempts):
        r = operation.get_experiment_status()
        if r and not r.get("error") and r.get("data"):
            return r["data"]["qtd_scheduler_total"]
        sleep(0.2)
    return None


def _read_is_correct(read_result) -> bool:
    if not read_result or read_result.get("error") or read_result.get("data") is None:
        return False
    return all(exp["ecc_telemetry"][c] == 0
               for exp in read_result["data"] for c in ECC_ZERO_FIELDS)


# --- Emulated flash ---

class Emulated_Flash:
    """Shared emulated flash for tests. All state is at the class level."""

    op = None
    list_write_cmd_exp = []
    list_readings_exp = []
    summary = []
    available_blk = set(range(0, 64))
    identifier = 0
    qtd_flash_page = 0

    @classmethod
    def set_operation(cls, operation):
        """Initialize Operation once for all tests."""
        cls.op = operation

    @classmethod
    def get_valid_write_commands(cls):
        """Number of write commands accepted."""
        return len(cls.list_write_cmd_exp)

    @classmethod
    def get_flash_page_qtd(cls):
        """Number of pages expected in the experiment summary."""
        return cls.qtd_flash_page

    @classmethod
    def get_summary(cls):
        return cls.summary

    @classmethod
    def append_summary(cls, summary_page):
        cls.summary.append(summary_page)

    @classmethod
    def count_appearances_in_summary(cls, cmd):
        count = 0
        for summary_page in cls.summary:
            data_section = summary_page.get("data", {})
            entries = data_section.get("entries", [])
            for entry in entries:
                if entry["id"] == cmd.id:
                    count += 1
        return count

    @classmethod
    def check_write_cmd(cls, cmd: NASCERR_CMD) -> bool:
        """
        Same rule as nascerr_schdb_checkSchedulerConflict: True if the command
        should be accepted. Does not allocate anything.
        """
        block = cmd.block
        qtd_block = cmd.qtd_block

        if block + qtd_block > 64:
            return False

        for i in range(block, block + qtd_block):
            if i not in cls.available_blk:
                return False
        return True

    @classmethod
    def register_write_cmd(cls, cmd: NASCERR_CMD):
        """Store a command accepted by the firmware and allocate its blocks."""
        for i in range(cmd.block, cmd.block + cmd.qtd_block):
            cls.available_blk.discard(i)

        cls.qtd_flash_page = cls.qtd_flash_page + cmd.repeat + 1
        cls.list_write_cmd_exp.append(cmd)

    @classmethod
    def handle_write_cmd(cls, cmd: NASCERR_CMD) -> bool:
        """Validate a command and, if valid, register it."""
        if not cls.check_write_cmd(cmd):
            return False
        cls.register_write_cmd(cmd)
        return True

    @classmethod
    def handle_summary(cls):
        """True if every accepted command appears repeat + 1 times in the summary."""
        cmd_list = cls.list_write_cmd_exp.copy()
        for cmd in cmd_list:
            repeat_cmd = Emulated_Flash.count_appearances_in_summary(cmd)
            if (repeat_cmd != (cmd.repeat + 1)):
                return False
        return True

    @classmethod
    def handle_read_cmd(cls, cmd: dict):
        """
        Mark the entry at global index cmd['block'] as 'READ', mapping it to a
        summary page through each page's 'offset' field.
        """
        global_index = cmd["block"]

        for page_index, page in enumerate(cls.summary):
            data = page.get("data", {})
            entries = data.get("entries", [])
            offset = data.get("offset", 0)

            if offset <= global_index < offset + len(entries):
                local_index = global_index - offset
                cls.summary[page_index]["data"]["entries"][local_index]["status"] = "READ"
                return True

        return False


# --- Tests ---

def test_experiment_write(operation: Operation):
    exp = FlashRegionTypeDef(FLASH_EXPERIMENT)
    sched = FlashRegionTypeDef(FLASH_SCHEDULER)

    print(operation.clear_database(sched))
    sleep(10)
    print(operation.clear_database(exp))
    sleep(10)

    cmd_list = []
    identifier = 0
    ecc = choice([NASCERR_MODE.MRSC, NASCERR_MODE.TBEC])
    write_type = choice([
        W_DataTypeDef.WRITE_x55h,
        W_DataTypeDef.WRITE_ONES,
        W_DataTypeDef.WRITE_ZEROS,
        W_DataTypeDef.WRITE_xAAh,
    ])
    scheduler_time = 120000
    delay = scheduler_time

    # Generated commands are not necessarily valid.
    available_blk = set(range(0, 64))

    while available_blk:
        blk = choice(list(available_blk))
        qtd_blk = randint(1, 3)
        repeat = randint(0, 3)

        for i in range(blk, blk + qtd_blk):
            available_blk.discard(i)

        identifier += 1
        cmd = NASCERR_CMD(
            cmd_type=CmdTypeDef.WRITE_CMD,
            id=identifier,
            block=blk,
            qtd_block=qtd_blk,
            ecc=ecc,
            write_type=write_type,
            scheduler_time=scheduler_time,
            repeat=repeat,
            delay=delay//4,
            current_tick=0,
        )
        cmd_list.append(cmd)

    # Outcome of each command:
    #   accepted -> qtd_scheduler_total increased by 1
    #   rejected -> counter unchanged and ACK = FAIL
    #   lost     -> counter unchanged and ACK = SUCESS (stale ACK: firmware did not register the write)
    #   no_ack   -> ACK did not reach SUCESS/FAIL within the timeout
    # The prediction uses the real state (only accepted commands take blocks),
    # so a mismatch does not propagate to the following commands.
    count_before = _scheduled_count(operation)
    assert count_before is not None, "Could not read EXP_STAT before sending"

    rows = []
    for cmd in cmd_list:
        predicted = Emulated_Flash.check_write_cmd(cmd)
        try:
            result = operation.send_commandNew(
                cmd_type=cmd.cmd_type,
                id=cmd.id,
                block=cmd.block,
                qtd_block=cmd.qtd_block,
                ecc=cmd.ecc,
                write_type=cmd.write_type,
                scheduler_time=cmd.scheduler_time,
                repeat=cmd.repeat,
                delay=cmd.delay,
                current_tick=cmd.current_tick,
            )
        except Exception as e:
            metrics.request(TEST_QUEUE, False, f"cmd {cmd.id} | exception: {e}")
            raise
        if result is None or result["error"] != False:
            metrics.request(TEST_QUEUE, False, f"cmd {cmd.id} | send error")
        assert result is not None and result["error"] == False

        ack = _wait_ack(operation)
        count_after = _wait_scheduled_count(operation, count_before + 1)

        if count_after is not None:
            entered = count_after > count_before
            count_before = count_after
        else:
            entered = ack == NASCERR_ACK.ACK_SUCESS
            count_before += int(entered)

        if ack is None:
            outcome = "no_ack"
        elif entered:
            outcome = "accepted"
        elif ack == NASCERR_ACK.ACK_FAIL:
            outcome = "rejected"
        else:
            outcome = "lost"

        if entered:
            Emulated_Flash.register_write_cmd(cmd)

        correct = (predicted and outcome == "accepted") or (not predicted and outcome == "rejected")
        metrics.request(TEST_QUEUE, correct,
                        f"cmd {cmd.id} | predicted={'accepted' if predicted else 'rejected'} "
                        f"| got={outcome}")
        rows.append((cmd, predicted, ack, outcome, correct, count_after))
        sleep(0.5)

    print("\nAll commands sent")
    print(f"{'id':>4} {'blocks':>9} {'predicted':>9} {'ACK':>12} {'counter':>9} {'got':>9}")
    for cmd, predicted, ack, outcome, correct, qtd in rows:
        blocks = f"{cmd.block}-{cmd.block + cmd.qtd_block - 1}"
        print(f"{cmd.id:>4} {blocks:>9} {'accepted' if predicted else 'rejected':>9} "
              f"{ack.name if ack else '-':>12} {qtd if qtd is not None else '?':>9} "
              f"{outcome:>9}{'' if correct else '   <-- MISMATCH'}")

    mismatches = [(c.id, r) for c, p, a, r, ok, q in rows if not ok]
    qtd_accepted = Emulated_Flash.get_valid_write_commands()
    qtd_predicted = sum(1 for c, p, *_ in rows if p)
    print(f"\nSent: {len(rows)} | predicted accepted: {qtd_predicted} | "
          f"accepted by firmware: {qtd_accepted} | mismatches: {len(mismatches)}")

    commands_on_flash = operation.get_experiment_status()
    print("commands_on_flash", commands_on_flash)
    assert commands_on_flash is not None and commands_on_flash["error"] == False
    qtd_commands_on_flash = commands_on_flash["data"]["qtd_scheduler_total"]
    assert qtd_accepted == qtd_commands_on_flash
    assert not mismatches, f"Commands with an outcome different from predicted: {mismatches}"


def test_experiment_summary(operation: Operation):
    qtd_page_to_read = Emulated_Flash.get_flash_page_qtd()
    print("qtd_page_to_read", qtd_page_to_read)

    experiments_on_flash = operation.get_experiment_status()
    assert experiments_on_flash is not None and experiments_on_flash["error"] == False
    qtd_experiments_on_flash = experiments_on_flash["data"]["qtd_experiments_unread"]
    sleep(1)

    while (qtd_page_to_read > qtd_experiments_on_flash):
        # Occasionally returns a CRC error, likely due to concurrent bus access.
        experiments_on_flash = operation.get_experiment_status()
        if experiments_on_flash is not None and experiments_on_flash["error"] == False:
            qtd_experiments_on_flash = experiments_on_flash["data"]["qtd_experiments_unread"]
        sleep(1)

    sleep(10)

    experiments_on_flash = operation.get_experiment_status()
    assert experiments_on_flash is not None and experiments_on_flash["error"] == False
    qtd_experiments_on_flash = experiments_on_flash["data"]["qtd_experiments_unread"]
    assert qtd_experiments_on_flash == qtd_page_to_read

    sleep(1)

    offset = 0
    qtd_sum_pages = (qtd_page_to_read // 30) + 1
    for i in range(0, qtd_sum_pages):
        operation.set_exp_sum_idx(offset)
        sleep(1)
        summary = operation.get_exp_sum()
        assert summary is not None and summary["error"] == False

        Emulated_Flash.append_summary(summary)
        offset = offset + 30
        print(summary)
        sleep(10)
    assert Emulated_Flash.handle_summary() == True


def test_experiment_read(operation: Operation):
    """
    Read every experiment listed in the collected summary and check that no
    block has ECC errors.

    Metric: each scheduled experiment (expected page) is one request, correct
    only if it was executed and read without ECC errors. Experiments never read
    (test interrupted earlier) count as incorrect.
    """
    recorded = []
    try:
        _run_read(operation, recorded)
    finally:
        expected = Emulated_Flash.get_flash_page_qtd()
        for i in range(len(recorded), expected):
            metrics.request(TEST_EXECUTION, False, f"not read ({i + 1}/{expected})")


def _run_read(operation: Operation, recorded: list):
    summary = Emulated_Flash.get_summary()
    qtd_experiments_finished = 0
    page = 0
    for i in range(len(summary)):
        entries = summary[i].get("data", {}).get("entries", [])
        for entry in entries:
            get_read_page = operation.send_commandNew(
                cmd_type=CmdTypeDef.READ_CMD,
                id=entry["id"],
                block=page,
                qtd_block=entry["num_block"],
                ecc=NASCERR_MODE.MRSC,
                write_type=0,
                scheduler_time=0,
                repeat=0,
                delay=0,
                current_tick=0,
            )
            assert get_read_page["error"] == False
            sleep(0.5)

            # Read progress is reported through REQUEST_EXP, not the ACK.
            print("Reading page:", page)
            req = operation.get_status()["data"]["request"]["EXP"]["value"]
            sleep(1)
            while req != REQUEST_EXP.REQ_EXP_TX_RESULT.value:
                req = operation.get_status()["data"]["request"]["EXP"]["value"]
                sleep(1)

            Emulated_Flash.handle_read_cmd(get_read_page["command_dict"])
            read_result = operation.read_experiment()
            recorded.append(metrics.request(
                TEST_EXECUTION, _read_is_correct(read_result), f"exp id {entry['id']}"))
            assert read_result["error"] == False and read_result["data"] is not None
            page = page + 1

            qtd_experiments_finished = qtd_experiments_finished + 1

            if page >= 64 and page == Emulated_Flash.get_flash_page_qtd():
                qtd_experiments_finished = 0
                sleep(3)
            sleep(2)
            assert qtd_experiments_finished == operation.get_experiment_status()[
                "data"]["qtd_experiments_finished"]

            for exp in read_result["data"]:
                ecc = exp["ecc_telemetry"]
                assert ecc["single_bit_errors"] == 0
                assert ecc["double_bit_errors"] == 0
                assert ecc["triple_bit_errors"] == 0
                assert ecc["beg_region_errors"] == 0
                assert ecc["mid_region_errors"] == 0
                assert ecc["end_region_errors"] == 0
                assert ecc["uncorrectable_errors"] == 0
                assert ecc["ecc_status"] == 0
            sleep(5)

    if page >= 64:
        page = 0
    print("Experiment summary in flash: \n", Emulated_Flash.get_summary())

    # Compare the local summary (with READ marks) against the one on flash.
    offset = 0
    qtd_sum_pages = (page // 30) + 1
    for i in range(0, qtd_sum_pages):
        operation.set_exp_sum_idx(offset)
        sleep(1)
        summary_on_flash = operation.get_exp_sum()
        assert summary_on_flash is not None and summary_on_flash["error"] == False

        for j in range(0, len(summary_on_flash["data"]["entries"])):
            print(
                f'summary[i]["data"]["entries"][j]["status"]: {summary[i]["data"]["entries"][j]}')
            print(
                f'summary_on_flash["data"]["entries"][j]: {summary_on_flash["data"]["entries"][j]}')
            assert summary[i]["data"]["entries"][j] == summary_on_flash["data"]["entries"][j]

        offset = offset + 30
        sleep(10)
