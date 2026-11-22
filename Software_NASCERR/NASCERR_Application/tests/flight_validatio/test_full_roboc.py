"""
Mission-style long-duration soak test for the NASCERR payload.

Schedules experiments in free flash blocks (BlockManager), reads them back through
the HardwareExperimentService state machine (expsum -> READ_CMD -> poll -> read_buffer)
and reports the soak metric T = correct / scheduled experiments.
"""

from __future__ import annotations

import csv
import json
import os
import random
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))  # project root (nascerr_modules, roboc_payload)
sys.path.insert(0, _TESTS_DIR)                   # tests/ (metrics)

from nascerr_modules.nascerr_operation import Operation
from nascerr_modules.nascerr_command import CmdTypeDef, W_DataTypeDef
from nascerr_modules.nascerr_system import (
    FlashRegionTypeDef,
    NASCERR_MODE,
    NASCERR_ACK,
)
from roboc_payload.experiments import HardwareExperimentService

import metrics

# --- Configuration ---

SOAK_TEST = "soak_execution"  # each scheduled experiment = 1 request

DEVICE_NAME = "NASCERR_CargaUtil"
I2C_ADDRESS = 0x55

OUT_DIR = "flight_soak_results"
SESSION_NAME = time.strftime("flight_soak_%Y%m%d_%H%M%S")

PHASE1_DURATION = 60.0 * 10  # s
PHASE2_DURATION = 60.0 * 20  # s

FW_PATH = "binario_firmware/dev_nascerr_firmware.bin"
FW_VERSION = 0x0020005

BATCH_PHASE1 = 20
BATCH_SOAK_MIN = 15
BATCH_SOAK_MAX = 30

CMD_MAX_BLOCKS = 6

MAX_RETRIES = 3
RETRY_BACKOFF = 0.3  # s

LOOP_TICK = 0.2       # s
READ_LOOP_TICK = 0.1  # s

PERIOD_HW = 1.0      # s
PERIOD_LOGS = 5.0    # s
PERIOD_STATUS = 1.0  # s
PERIOD_EXP_POLL = 1.0  # s

SOAK_INJECT_MIN = 10  # s between injections of new experiments
SOAK_INJECT_MAX = 25  # s

CMD_SEND_SLEEP_MIN = 0.02  # s
CMD_SEND_SLEEP_MAX = 0.08  # s

# Only WRITE_CMD has an ACK; READ_CMD does not.
ACK_TIMEOUT = 5.0  # s
ACK_POLL = 0.05    # s

# Final drain: read until nothing is pending or this limit is reached.
DRAIN_MAX = 60.0 * 15  # s

ECC_ZERO_FIELDS = ("single_bit_errors", "double_bit_errors", "triple_bit_errors",
                   "beg_region_errors", "mid_region_errors", "end_region_errors",
                   "uncorrectable_errors", "ecc_status")


# --- Terminal logger ---

class SoakLogger:
    """Prints `[HH:MM:SS] [PHASE] [LEVEL] message` lines."""

    _LEVELS = {
        "INFO ": "",
        "OK   ": "✓ ",
        "WARN ": "⚠  ",
        "FAIL ": "✗ ",
        "ERROR": "✗✗ ",
        "STAT ": "   ",
        "EVENT": "→ ",
    }

    def __init__(self):
        self._phase = "INIT"
        self._lock = threading.Lock()

    def set_phase(self, phase: str):
        with self._lock:
            self._phase = phase
        sep = "═" * 64
        print(f"\n{sep}")
        print(f"  ▶  PHASE: {phase:<30} {time.strftime('%H:%M:%S')}")
        print(f"{sep}")

    def _write(self, level: str, msg: str):
        prefix = self._LEVELS.get(level, "")
        ts = time.strftime("%H:%M:%S")
        phase = self._phase
        with self._lock:
            print(f"[{ts}] [{phase:<18}] [{level}] {prefix}{msg}")

    def info(self, msg): self._write("INFO ", msg)
    def ok(self, msg): self._write("OK   ", msg)
    def warn(self, msg): self._write("WARN ", msg)
    def fail(self, msg): self._write("FAIL ", msg)
    def error(self, msg): self._write("ERROR", msg)
    def stat(self, label, value): self._write("STAT ", f"{label}: {value}")
    def event(self, msg): self._write("EVENT", msg)

    def summary_block(self, title: str, items: Dict[str, Any]):
        print(f"\n  ┌─ {title}")
        for k, v in items.items():
            print(f"  │  {k:<32} {v}")
        print(f"  └{'─'*48}")


# --- Block manager ---

@dataclass
class _Allocation:
    start_block: int
    qtd_block: int
    cmd_id: int


class BlockManager:
    """Tracks which of the 64 flash blocks are free; allocations must be contiguous."""

    TOTAL = 64

    def __init__(self, log: SoakLogger):
        self._free: set[int] = set(range(self.TOTAL))
        self._allocs: List[_Allocation] = []
        self._lock = threading.Lock()
        self._log = log

    def allocate(self, qtd_block: int, cmd_id: int) -> Optional[int]:
        """Reserve qtd_block contiguous free blocks; return the start block or None."""
        with self._lock:
            free_sorted = sorted(self._free)
            start = None

            for i in range(len(free_sorted) - qtd_block + 1):
                if all(free_sorted[i + j] == free_sorted[i] + j
                       for j in range(qtd_block)):
                    start = free_sorted[i]
                    break

            if start is None:
                return None

            for b in range(start, start + qtd_block):
                self._free.remove(b)
            self._allocs.append(_Allocation(start, qtd_block, cmd_id))
            return start

    def release_by_id(self, cmd_id: int):
        with self._lock:
            for alloc in list(self._allocs):
                if alloc.cmd_id == cmd_id:
                    for b in range(alloc.start_block,
                                   alloc.start_block + alloc.qtd_block):
                        self._free.add(b)
                    self._allocs.remove(alloc)
                    return

    def release_all(self):
        with self._lock:
            self._free = set(range(self.TOTAL))
            self._allocs.clear()

    @property
    def free_count(self) -> int:
        with self._lock:
            return len(self._free)

    @property
    def used_count(self) -> int:
        return self.TOTAL - self.free_count

    def bar(self) -> str:
        """█ = used, ░ = free (64 chars)."""
        with self._lock:
            return "".join("█" if i not in self._free else "░"
                           for i in range(self.TOTAL))

    def status_line(self) -> str:
        return f"free={self.free_count:2d}/64  [{self.bar()}]"


# --- Utilities ---

@dataclass
class Event:
    ts: float
    session: str
    phase: str
    task: str
    name: str
    ok: bool
    duration_ms: int
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class JsonlRecorder:
    def __init__(self, out_dir: str, session: str):
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, f"{session}.jsonl")
        self._lock = threading.Lock()

    def write(self, ev: Event):
        line = json.dumps(asdict(ev), ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


class Metrics:
    def __init__(self):
        self.count_ok = 0
        self.count_fail = 0
        self.by_name: Dict[str, Dict[str, int]] = {}

    def add(self, name: str, ok: bool):
        if ok:
            self.count_ok += 1
        else:
            self.count_fail += 1
        if name not in self.by_name:
            self.by_name[name] = {"ok": 0, "fail": 0}
        self.by_name[name]["ok" if ok else "fail"] += 1


def now() -> float:
    return time.time()


def safe_call(fn, retries=MAX_RETRIES, backoff=RETRY_BACKOFF):
    last = None
    for i in range(retries):
        try:
            r = fn()
            last = r
            if isinstance(r, dict) and r.get("error") is False:
                return r
        except Exception as e:
            last = {"error": True, "log_error": f"exception: {e}"}
        time.sleep(backoff * (i + 1))
    return last or {"error": True, "log_error": "unknown"}


# --- Flight soak ---

class FlightSoak:
    """Mission runner: periodic telemetry, experiment scheduling and read-back."""

    def __init__(self, operation: Operation,
                 recorder: JsonlRecorder,
                 log: SoakLogger):
        self.op = operation
        self.rec = recorder
        self.log = log
        self.metrics = Metrics()
        self.phase = "INIT"
        self.stop_flag = False

        # Shared between FlightSoak and HardwareExperimentService.
        self._hw_lock = threading.Lock()

        self.blocks = BlockManager(log)

        self._exp_svc = HardwareExperimentService(
            op=self.op,
            hw_lock=self._hw_lock,
        )

        self._exp_read_total = 0
        self._exp_read_errors = 0

        # Scheduled experiments awaiting read-back:
        #   cmd_id -> {"expected": repeat+1, "qtd_block": n, "pages": set(), "ok": bool, "faults": []}
        # Each read_next() batch is one flash page = one execution, with one result per
        # block. A re-read returns the same page; a new execution is a new page.
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._exp_scheduled = 0
        self._exp_correct = 0
        self._exp_incorrect = 0
        self._unexpected_reads = 0
        self._rereads = 0
        self._unread_in_summary = 0
        self._unread_not_in_summary = 0

        # IDs seen in the experiment summary, captured when the read state machine
        # itself loads the summary (no extra I2C reads).
        self._ids_in_summary: Dict[int, int] = {}
        dbg = getattr(self._exp_svc, "_dbg", None)
        if dbg is not None and hasattr(dbg, "summary_loaded"):
            original = dbg.summary_loaded

            def peek_summary(offset, page, entries, _orig=original):
                for e in entries or []:
                    try:
                        i = int(e.get("id"))
                    except (TypeError, ValueError):
                        continue
                    self._ids_in_summary[i] = self._ids_in_summary.get(i, 0) + 1
                return _orig(offset, page, entries)

            dbg.summary_loaded = peek_summary
        else:
            self.log.warn("Cannot monitor the summary (no _dbg.summary_loaded)")

        self._last_log_pages = None

    def emit(self, task, name, ok, t0,
             data=None, err=None):
        ev = Event(
            ts=now(), session=SESSION_NAME, phase=self.phase,
            task=task, name=name, ok=ok,
            duration_ms=int((now() - t0) * 1000),
            data=data, error=err,
        )
        self.rec.write(ev)
        self.metrics.add(f"{task}:{name}", ok)

    # --- Per-experiment tracking (soak metric T) ---

    def _wait_ack(self) -> Optional[NASCERR_ACK]:
        """Wait for the WRITE_CMD ACK to reach SUCESS or FAIL. None = timeout."""
        deadline = now() + ACK_TIMEOUT
        while now() < deadline:
            with self._hw_lock:
                r = self.op.get_ack()
            if r and not r.get("error") and r.get("data"):
                value = r["data"]["ACK info"]["value"]
                if value in (NASCERR_ACK.ACK_SUCESS, NASCERR_ACK.ACK_FAIL):
                    return NASCERR_ACK(value)
            time.sleep(ACK_POLL)
        return None

    def _close_experiment(self, cmd_id: int, correct: bool, reason: str):
        """Record a scheduled experiment in the metric and free its blocks."""
        self._pending.pop(cmd_id, None)
        self.blocks.release_by_id(cmd_id)
        if correct:
            self._exp_correct += 1
        else:
            self._exp_incorrect += 1
        metrics.request(SOAK_TEST, correct, f"exp id {cmd_id} | {reason}")

    def _process_batch(self, batch, source: str) -> int:
        """Handle one read_next() batch (one flash page); return the ECC error count."""
        page = self._exp_svc._state.exp_flash_page - 1  # page that was just read
        ecc_errors = 0
        by_exp: Dict[int, list] = {}

        for res in batch.results:
            self._exp_read_total += 1
            ecc = res.ecc_telemetry or {}
            ecc_ok = all(ecc.get(c, 0) == 0 for c in ECC_ZERO_FIELDS)
            if not ecc_ok:
                ecc_errors += 1
                self._exp_read_errors += 1
                self.log.warn(f"[{source}] ECC error  id={res.experiment_id} "
                              f"blk={res.block}  page={page}  ecc={ecc}")
            else:
                self.log.ok(f"[{source}] read  id={res.experiment_id} "
                            f"blk={res.block}  page={page}  errors={res.number_errors}  "
                            f"pct_error={res.percent_error:.2f}%")
            by_exp.setdefault(res.experiment_id, []).append((res, ecc_ok))

        for exp_id, items in by_exp.items():
            p = self._pending.get(exp_id)
            if p is None:
                self._unexpected_reads += 1
                self.log.warn(f"[{source}] page {page} of id={exp_id} "
                              f"which is not pending")
                continue
            if page in p["pages"]:
                self._rereads += 1
                self.log.info(f"[{source}] re-read of page {page} of id={exp_id} (ignored)")
                continue
            p["pages"].add(page)

            lost = any((r.mdca_data or {}).get("error") for r, _ in items)
            blocks = {r.block for r, _ in items}
            exec_ok = (not lost and len(blocks) == p["qtd_block"]
                       and all(ok for _, ok in items))
            if not exec_ok:
                p["faults"].append(
                    f"page {page}: " + ("lost (CRC)" if lost else
                                        f"{len(blocks)}/{p['qtd_block']} blocks"
                                        if len(blocks) != p["qtd_block"] else "ECC error"))
            p["ok"] = p["ok"] and exec_ok

            # The firmware frees the blocks (DB_CANCELED) only after the last execution.
            if len(p["pages"]) >= p["expected"]:
                self._close_experiment(
                    exp_id, p["ok"],
                    f"{len(p['pages'])}/{p['expected']} executions read"
                    + ("" if p["ok"] else " | " + "; ".join(p["faults"])))
        return ecc_errors

    def _close_pending(self, reason: str):
        """Count scheduled experiments that were never read as incorrect."""
        for cmd_id, p in list(self._pending.items()):
            times = self._ids_in_summary.get(cmd_id, 0)
            if times:
                self._unread_in_summary += 1
            else:
                self._unread_not_in_summary += 1
            self.log.warn(f"unread: id={cmd_id} blocks={p['qtd_block']} "
                          f"executions={len(p['pages'])}/{p['expected']} "
                          f"| in summary: {'yes (' + str(times) + 'x)' if times else 'no'}")
            self._close_experiment(
                cmd_id, False,
                f"{reason} | {len(p['pages'])}/{p['expected']} executions read "
                f"| in summary: {'yes' if times else 'no'}")

    # --- Periodic tasks ---

    def task_status(self):
        t0 = now()
        r = safe_call(lambda: self.op.get_status())
        ok = r and r.get("error") is False
        data, err = None, None
        if ok:
            d = r.get("data", {})
            data = {
                "connection_status": r.get("connection_status"),
                "flags": d.get("flags"),
                "request": d.get("request"),
            }
            req_exp = d.get("request", {}).get("EXP", {})
            self.log.info(
                f"status OK  | "
                f"conn={r.get('connection_status')}  "
                f"EXP={req_exp.get('name','?')}"
            )
        else:
            err = r.get("log_error") if isinstance(r, dict) else "unknown"
            self.log.fail(f"task_status — {err}")
        self.emit("periodic", "status", ok, t0, data=data, err=err)

    def task_hw(self):
        t0 = now()
        out = {}
        ok = True
        err = None

        rT = safe_call(lambda: self.op.get_hardware_temperature())
        if rT and rT.get("error") is False:
            out["temperature"] = rT.get("data")
        else:
            ok = False
            err = f"temp: {rT.get('log_error','?') if isinstance(rT,dict) else '?'}"

        rI = safe_call(lambda: self.op.get_hardware_total_current())
        if rI and rI.get("error") is False:
            out["current_total_mA"] = rI.get("data")
        else:
            ok = False
            err = (err or "") + f" | current: {rI.get('log_error','?') if isinstance(rI,dict) else '?'}"

        rP = safe_call(lambda: self.op.get_hardware_total_power())
        if rP and rP.get("error") is False:
            out["power_total_mW"] = rP.get("data")
        else:
            ok = False
            err = (err or "") + f" | power: {rP.get('log_error','?') if isinstance(rP,dict) else '?'}"

        if ok:
            temp = out.get("temperature", [None])[0]
            cur = out.get("current_total_mA", [None])[0]
            pwr = out.get("power_total_mW", [None])[0]
            self.log.stat(
                "hw",
                f"T={temp:.1f}°C  I={cur:.1f}mA  P={pwr:.1f}mW"
                if None not in (temp, cur, pwr) else str(out)
            )
        else:
            self.log.fail(f"task_hw — {err}")

        self.emit("periodic", "hardware", ok, t0,
                  data=out if out else None, err=err)

    def task_logs(self):
        t0 = now()
        ok = True
        err = None
        out = {}

        summary = safe_call(lambda: self.op.get_log_summary())
        if summary and summary.get("error") is False:
            total_pages = summary.get("data", {}).get("total_pages")
            out["total_pages"] = total_pages
            if total_pages != self._last_log_pages:
                self._last_log_pages = total_pages
                self.log.event(f"logs — total_pages changed to {total_pages}")
            else:
                self.log.stat("logs", f"total_pages={total_pages} (unchanged)")
        else:
            ok = False
            err = f"log_summary: {summary.get('log_error','?') if isinstance(summary,dict) else '?'}"
            self.log.fail(f"task_logs — {err}")

        self.emit("periodic", "logs", ok, t0, data=out or None, err=err)

    def task_exp_poll(self):
        t0 = now()
        r = safe_call(lambda: self.op.get_experiment_status())
        ok = r and r.get("error") is False
        data, err = None, None
        if ok:
            d = r.get("data", {})
            data = {
                "qtd_scheduler_total": d.get("qtd_scheduler_total"),
                "qtd_scheduler_pending": d.get("qtd_scheduler_pending"),
                "qtd_scheduler_executed": d.get("qtd_scheduler_executed"),
                "qtd_experiments_unread": d.get("qtd_experiments_unread"),
                "qtd_experiments_finished": d.get("qtd_experiments_finished"),
                "flash_full_flag": d.get("flash_full_flag"),
            }
            self.log.stat(
                "exp_poll",
                f"unread={data['qtd_experiments_unread']}  "
                f"finished={data['qtd_experiments_finished']}  "
                f"total={data['qtd_scheduler_total']}"
            )
        else:
            err = r.get("log_error", "?") if isinstance(r, dict) else "?"
            self.log.fail(f"task_exp_poll — {err}")
        self.emit("periodic", "exp_poll", ok, t0, data=data, err=err)

    # --- Mission actions ---

    def action_clear_regions(self):
        t0 = now()
        self.log.event("Clearing flash regions (LOGS + SCHEDULER + EXPERIMENT)...")
        ok = True
        err = None
        out = {}

        regions = [("clear_logs", FlashRegionTypeDef.FLASH_LOGS),
                   ("clear_sched", FlashRegionTypeDef.FLASH_SCHEDULER),
                   ("clear_exp", FlashRegionTypeDef.FLASH_EXPERIMENT)]
        for name, region in regions:
            r = safe_call(lambda: self.op.clear_database(region))
            if not (r and r.get("error") is False):
                ok = False
                err = (err + " | " if err else "") + f"{name}: {r.get('log_error','?')}"
            time.sleep(10.0)

        if ok:
            self.blocks.release_all()
            self._exp_svc.sync_initial_state()
            self.log.ok("Regions cleared. BlockManager reset and reader synchronized.")
        else:
            self.log.fail(f"action_clear_regions — {err}")

        self.emit("action", "clear_regions", ok, t0, data=out, err=err)

    def action_schedule_batch(self, n_cmds: int = 10) -> int:
        """Schedule n_cmds experiments in free contiguous blocks; return how many were sent."""
        t0 = now()
        self.log.event(f"Scheduling batch of {n_cmds} experiment(s)...")

        ecc_modes = [NASCERR_MODE.MRSC, NASCERR_MODE.TBEC]
        write_types = [
            W_DataTypeDef.WRITE_xAAh,
            W_DataTypeDef.WRITE_x55h,
            W_DataTypeDef.WRITE_ONES,
            W_DataTypeDef.WRITE_ZEROS,
        ]

        sent = 0
        failed = 0
        skipped_blocks = 0

        for i in range(n_cmds):
            if self.stop_flag:
                break

            qtd_block = random.randint(1, CMD_MAX_BLOCKS)

            if self.blocks.free_count < qtd_block:
                skipped_blocks += 1
                self.log.warn(
                    f"  [{i+1}/{n_cmds}] No free blocks "
                    f"(need={qtd_block} free={self.blocks.free_count}). "
                    f"Skipping."
                )
                continue

            cmd_id = (int(now() * 1000) + i) & 0xFFFFFF

            start_block = self.blocks.allocate(qtd_block, cmd_id)
            if start_block is None:
                skipped_blocks += 1
                self.log.warn(
                    f"  [{i+1}/{n_cmds}] BlockManager found no "
                    f"{qtd_block} contiguous block(s). Skipping."
                )
                continue

            ecc = random.choice(ecc_modes)
            write_type = random.choice(write_types)
            repeat = random.randint(0, 2)

            # 90% immediate, 10% scheduled
            if random.random() < 0.9:
                scheduler_time = 1
            else:
                scheduler_time = random.randint(500, 10000)

            delay = scheduler_time // max(random.randint(2, 4), 1)

            self.log.info(
                f"  [{i+1}/{n_cmds}] id={cmd_id} "
                f"blk={start_block}+{qtd_block}  "
                f"ecc={ecc.name}  wt={write_type.name}  "
                f"sched={scheduler_time}ms  repeat={repeat}"
            )

            with self._hw_lock:
                r = safe_call(lambda: self.op.send_commandNew(
                    cmd_type=CmdTypeDef.WRITE_CMD,
                    id=cmd_id,
                    block=start_block,
                    qtd_block=qtd_block,
                    ecc=ecc,
                    write_type=write_type,
                    scheduler_time=scheduler_time,
                    repeat=repeat,
                    delay=delay,
                    current_tick=0,
                ))

            self._exp_scheduled += 1
            ack = self._wait_ack() if (r and r.get("error") is False) else None

            if not (r and r.get("error") is False):
                failed += 1
                self._close_experiment(cmd_id, False, "send error")
                self.log.fail(
                    f"  [{i+1}/{n_cmds}] id={cmd_id} SEND ERROR "
                    f"— {r.get('log_error','?') if isinstance(r,dict) else '?'}"
                )
            elif ack == NASCERR_ACK.ACK_FAIL:
                # BlockManager follows the firmware's own allocation rule, so FAIL here is a divergence.
                failed += 1
                self._close_experiment(cmd_id, False, "rejected by firmware (ACK_FAIL)")
                self.log.fail(
                    f"  [{i+1}/{n_cmds}] id={cmd_id} REJECTED (ACK_FAIL) "
                    f"| blocks {start_block}–{start_block+qtd_block-1}"
                )
            else:
                # SUCESS or ACK timeout: stays pending and the read-back decides.
                # A lost command (stale ACK) is never read and counts as incorrect.
                sent += 1
                self._pending[cmd_id] = {"expected": repeat + 1, "qtd_block": qtd_block,
                                         "pages": set(), "ok": True, "faults": []}
                self.log.ok(
                    f"  [{i+1}/{n_cmds}] id={cmd_id} "
                    f"{'accepted' if ack else 'no ACK (pending)'} "
                    f"| blocks {start_block}–{start_block+qtd_block-1}"
                )

            time.sleep(random.uniform(CMD_SEND_SLEEP_MIN, CMD_SEND_SLEEP_MAX))

        ok = failed == 0
        self.log.summary_block("Batch done", {
            "sent": sent,
            "failed": failed,
            "blocks_skipped": skipped_blocks,
            "blocks_free": self.blocks.free_count,
            "map": self.blocks.bar(),
        })
        self.emit("action", "schedule_batch", ok, t0,
                  data={"n_cmds": n_cmds, "sent": sent,
                        "failed": failed, "skipped_blocks": skipped_blocks},
                  err=None if ok else f"{failed} failure(s)")
        return sent

    def action_read_loop(self, max_duration_s: float = 30.0):
        """Run the HardwareExperimentService read state machine for up to max_duration_s."""
        t0 = now()
        batches = 0
        results = 0
        errors = 0

        self.log.event(
            f"Starting read cycle (max={max_duration_s:.0f}s) ..."
        )

        while not self.stop_flag and (now() - t0) < max_duration_s:
            batch = self._exp_svc.read_next()

            if batch is not None:
                batches += 1
                results += len(batch.results)
                errors += self._process_batch(batch, "read")

                self.log.stat(
                    "accumulated reads",
                    f"total={self._exp_read_total}  "
                    f"ecc_errors={self._exp_read_errors}  "
                    f"blocks_free={self.blocks.free_count}"
                )

            time.sleep(READ_LOOP_TICK)

        elapsed = now() - t0
        ok = errors == 0
        self.log.summary_block("Read cycle finished", {
            "duration (s)": f"{elapsed:.1f}",
            "batches": batches,
            "results": results,
            "ecc_errors": errors,
            "total_read": self._exp_read_total,
        })
        self.emit("action", "read_loop", ok, t0,
                  data={"batches": batches, "results": results,
                        "ecc_errors": errors,
                        "total_read": self._exp_read_total},
                  err=None if ok else f"{errors} ECC error(s)")

    def action_firmware_update(self):
        t0 = now()
        self.log.event(f"Starting OTA — firmware={FW_PATH}  version={hex(FW_VERSION)}")
        ok = True
        err = None
        out = {}
        try:
            time.sleep(2.0)
            result = self.op.update_firmware(
                FW_PATH, new_version=FW_VERSION, force=True)
            summary = result.get("summary") if isinstance(result, dict) else result
            out["update_summary"] = summary
            if not summary or not summary.get("success", False):
                ok = False
                err = "update_firmware returned success=False"
            else:
                self.log.ok(
                    f"OTA OK — "
                    f"pkts={summary.get('total_packets')}  "
                    f"time={summary.get('total_time_seconds'):.1f}s"
                )
        except Exception as e:
            ok = False
            err = f"exception: {e}\n{traceback.format_exc()}"
            self.log.error(f"OTA failed — {e}")
        self.emit("action", "firmware_update", ok, t0,
                  data=out or None, err=err)

    # --- Periodic telemetry loop ---

    def _run_periodic(self, duration_s: float):
        """Run periodic telemetry for duration_s, with one read tick per loop."""
        t_end = now() + duration_s
        next_hw = now()
        next_logs = now()
        next_st = now()
        next_exp = now()

        while not self.stop_flag and now() < t_end:
            t = now()

            if t >= next_st:
                self.task_status()
                next_st = t + PERIOD_STATUS

            if t >= next_hw:
                self.task_hw()
                next_hw = t + PERIOD_HW

            if t >= next_logs:
                self.task_logs()
                next_logs = t + PERIOD_LOGS

            if t >= next_exp:
                self.task_exp_poll()
                next_exp = t + PERIOD_EXP_POLL

            batch = self._exp_svc.read_next()
            if batch:
                self._process_batch(batch, "periodic")

            time.sleep(LOOP_TICK)

    # --- Main run ---

    def run(self):
        os.makedirs(OUT_DIR, exist_ok=True)

        self.phase = "INIT"
        self.log.set_phase(self.phase)
        t0 = now()
        self.emit("meta", "start", True, t0,
                  data={"device": DEVICE_NAME, "addr": hex(I2C_ADDRESS)})

        self.log.info("Synchronizing initial state with hardware...")
        self._exp_svc.sync_initial_state()
        self.log.ok("State synchronized.")

        self.phase = "PREP"
        self.log.set_phase(self.phase)
        self.action_clear_regions()
        time.sleep(2.0)

        self.phase = "PHASE1_NOMINAL"
        self.log.set_phase(self.phase)
        self.action_schedule_batch(n_cmds=BATCH_PHASE1)
        self.log.info(f"Nominal loop for {PHASE1_DURATION:.0f}s ...")
        self._run_periodic(PHASE1_DURATION)
        self.action_read_loop(max_duration_s=20.0)

        self.phase = "PHASE2_SOAK"
        self.log.set_phase(self.phase)
        t_soak_end = now() + PHASE2_DURATION
        next_inject = now()
        cycle = 0

        while not self.stop_flag and now() < t_soak_end:
            cycle += 1
            remaining = t_soak_end - now()
            self.log.info(
                f"[soak] cycle={cycle}  "
                f"remaining={remaining:.0f}s  "
                f"read={self._exp_read_total}  "
                f"ecc_errors={self._exp_read_errors}  "
                f"blocks_free={self.blocks.free_count}"
            )

            self._run_periodic(min(10.0, remaining))

            if now() >= next_inject:
                n = random.randint(BATCH_SOAK_MIN, BATCH_SOAK_MAX)
                sent = self.action_schedule_batch(n_cmds=n)
                self.action_read_loop(max_duration_s=15.0)
                next_inject = now() + random.randint(
                    SOAK_INJECT_MIN, SOAK_INJECT_MAX)

        # Drain: read what is still pending without scheduling anything new.
        self.phase = "DRAIN"
        self.log.set_phase(self.phase)
        t_drain_end = now() + DRAIN_MAX
        self.log.info(f"Draining {len(self._pending)} pending experiment(s) "
                      f"(max {DRAIN_MAX:.0f}s) ...")
        while not self.stop_flag and self._pending and now() < t_drain_end:
            self._run_periodic(min(10.0, t_drain_end - now()))
            self.log.info(f"[drain] pending={len(self._pending)}  "
                          f"remaining={t_drain_end - now():.0f}s")
        self._close_pending("not read by the end of the drain")

        self.finish()

    def finish(self):
        """Final summary: soak metric T and result files."""
        self._close_pending("test ended before read-back")
        self.phase = "DONE"
        self.log.set_phase(self.phase)
        t1 = now()
        T = (self._exp_correct / self._exp_scheduled) if self._exp_scheduled else None
        self.log.summary_block("FINAL RESULT", {
            "operations OK": self.metrics.count_ok,
            "operations FAIL": self.metrics.count_fail,
            "experiments scheduled": self._exp_scheduled,
            "executed and read (ok)": self._exp_correct,
            "incorrect/unread": self._exp_incorrect,
            "T = N_corr/N_sent": f"{100 * T:.2f}%" if T is not None else "-",
            "  unread, ID in summary": self._unread_in_summary,
            "  unread, not in summary": self._unread_not_in_summary,
            "results read (blocks)": self._exp_read_total,
            "re-reads ignored": self._rereads,
            "ECC errors": self._exp_read_errors,
            "unexpected pages": self._unexpected_reads,
            "IDs seen in summary": len(self._ids_in_summary),
            "free blocks": self.blocks.free_count,
        })
        self.emit("meta", "end", True, t1, data={
            "total_ok": self.metrics.count_ok,
            "total_fail": self.metrics.count_fail,
            "exp_scheduled": self._exp_scheduled,
            "exp_correct": self._exp_correct,
            "T": T,
            "exp_read": self._exp_read_total,
            "exp_ecc_err": self._exp_read_errors,
            "by_name": self.metrics.by_name,
        })
        self._write_summary_csv()

    def _write_summary_csv(self):
        path = os.path.join(OUT_DIR, f"{SESSION_NAME}_summary.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["metric", "ok", "fail"])
            for k, v in sorted(self.metrics.by_name.items()):
                w.writerow([k, v["ok"], v["fail"]])
        self.log.ok(f"Summary CSV saved: {path}")


# --- Main ---

def main():
    log = SoakLogger()
    op = Operation(DEVICE_NAME, I2C_ADDRESS)
    recorder = JsonlRecorder(OUT_DIR, SESSION_NAME)
    soak = FlightSoak(op, recorder, log)

    try:
        soak.run()
        log.ok(f"Test finished. JSONL: {OUT_DIR}/{SESSION_NAME}.jsonl")
    except KeyboardInterrupt:
        soak.stop_flag = True
        log.warn("Interrupted by user.")
        soak.finish()
    except Exception as e:
        log.error(f"Fatal error: {e}")
        print(traceback.format_exc())
        soak.emit("meta", "fatal_error", False, now(), err=str(e))
        soak.finish()


if __name__ == "__main__":
    main()
