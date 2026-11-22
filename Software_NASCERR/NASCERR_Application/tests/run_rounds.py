#!/usr/bin/env python3
"""
Run the NASCERR test suites for N rounds and compute the evaluation metrics:

    T = N_corr / N_env   (correct-response rate, functional campaign)
    C = N_det  / N_inj   (detection coverage, fault-injection campaign)

Usage (from the project root, with .venv active):

    python tests/run_rounds.py -n 10
    python tests/run_rounds.py -n 5 -s hardware bootloader
    python tests/run_rounds.py -n 3 --round-timeout 3600 -- -k "not out_of_order"

Everything after "--" is passed to pytest.
Output goes to results/<timestamp>/: metrics.jsonl, logs, JUnit XMLs and summary.json.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUITES = {
    "hardware":   "tests/test_hardware.py",
    "experiment": "tests/test_experiment.py",
    "bootloader": "tests/test_bootloader.py",
    "logs":       "tests/test_logs.py",
    "complet":    "tests/test_complet.py",
}
DEFAULT_SUITES = ["hardware", "experiment", "bootloader"]

# Continuous-load test; runs as a plain script, not through pytest
SOAK_SCRIPT = "tests/flight_validatio/test_full_roboc.py"

# Require switching the board off by hand during the test
POWER_TESTS = [
    "tests/test_bootloader.py::test_power_loss_during_reception",
    "tests/test_bootloader.py::test_power_loss_during_verification",
]
# Flashes a firmware image onto the device
UPDATE_TESTS = [
    "tests/test_bootloader.py::test_full_update_process",
]


def parse_args():
    p = argparse.ArgumentParser(
        description="Run the NASCERR tests for N rounds and compute T and C.")
    p.add_argument("-n", "--rounds", type=int, default=1,
                   help="number of rounds (default: 1)")
    p.add_argument("-s", "--suites", nargs="+", choices=[*SUITES.keys(), "soak"],
                   default=DEFAULT_SUITES,
                   help=f"suites to run (default: {' '.join(DEFAULT_SUITES)})")
    p.add_argument("--with-power", action="store_true",
                   help="include the power-loss tests (require manual intervention)")
    p.add_argument("--with-update", action="store_true",
                   help="include test_full_update_process (flashes firmware onto the device)")
    p.add_argument("--interval", type=float, default=5.0,
                   help="seconds to wait between rounds (default: 5)")
    p.add_argument("--round-timeout", type=float, default=None,
                   help="maximum duration of one round in seconds; on expiry the round "
                        "is stopped and recorded as a timeout")
    p.add_argument("--output", type=Path, default=None,
                   help="output directory (default: results/<timestamp>)")
    p.add_argument("pytest_args", nargs=argparse.REMAINDER,
                   help="extra pytest arguments, after --")
    a = p.parse_args()
    if a.pytest_args and a.pytest_args[0] == "--":
        a.pytest_args = a.pytest_args[1:]
    return a


def _run(cmd, env, log, limit_s, expired):
    """Run cmd, mirroring its output to the terminal and to log. Return the exit code."""
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)

    def kill_on_timeout():
        expired.set()
        proc.kill()

    timer = None
    if limit_s is not None:
        timer = threading.Timer(max(limit_s, 0), kill_on_timeout)
        timer.start()
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        proc.wait()
    except KeyboardInterrupt:
        # The child also gets Ctrl+C; give it time to print its own summary
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                log.write(line)
            proc.wait(timeout=60)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
        raise
    finally:
        if timer:
            timer.cancel()
    return proc.returncode


def run_round(round_no, args, output, metrics_file):
    junit = output / f"round_{round_no:03d}.xml"
    log_path = output / f"round_{round_no:03d}.log"

    files = [SUITES[s] for s in args.suites if s in SUITES]
    commands = []
    if files:
        cmd = [sys.executable, "-m", "pytest", "-v", "-s", "-p", "no:cacheprovider",
               f"--junitxml={junit}"]
        cmd += files
        if not args.with_power:
            cmd += [f"--deselect={t}" for t in POWER_TESTS]
        if not args.with_update:
            cmd += [f"--deselect={t}" for t in UPDATE_TESTS]
        cmd += args.pytest_args
        commands.append(cmd)
    if "soak" in args.suites:
        commands.append([sys.executable, SOAK_SCRIPT])

    env = dict(os.environ, NASCERR_METRICS=str(metrics_file), NASCERR_ROUND=str(round_no),
               PYTHONUNBUFFERED="1")

    start = time.time()
    deadline = start + args.round_timeout if args.round_timeout else None
    expired = threading.Event()
    code = 0
    with open(log_path, "w", buffering=1) as log:
        for cmd in commands:
            if expired.is_set():
                break
            limit = (deadline - time.time()) if deadline else None
            code = _run(cmd, env, log, limit, expired) or code
        if expired.is_set():
            msg = f"\n!!! Round {round_no} stopped by timeout ({args.round_timeout:.0f}s)\n"
            sys.stdout.write(msg)
            log.write(msg)

    return {
        "round": round_no,
        "duration_s": round(time.time() - start, 1),
        "exit_code": code,
        "timeout": expired.is_set(),
        "tests": read_junit(junit),
    }


def read_junit(path):
    res = {"passed": 0, "failed": 0, "skipped": 0, "failures": []}
    if not path.exists():
        return res
    for tc in ET.parse(path).iter("testcase"):
        tags = {c.tag for c in tc}
        if "skipped" in tags:
            res["skipped"] += 1
        elif tags & {"failure", "error"}:
            res["failed"] += 1
            res["failures"].append(tc.get("name"))
        else:
            res["passed"] += 1
    return res


def ratio(a, b):
    return a / b if b else None


def mean_stdev(values):
    values = [v for v in values if v is not None]
    if not values:
        return None, None
    return statistics.mean(values), (statistics.stdev(values) if len(values) > 1 else 0.0)


def compute_metrics(metrics_file, rounds_run):
    events = []
    if metrics_file.exists():
        with open(metrics_file) as f:
            events = [json.loads(line) for line in f if line.strip()]

    # test -> round -> counters
    req = defaultdict(lambda: defaultdict(lambda: {"env": 0, "corr": 0}))
    flt = defaultdict(lambda: defaultdict(lambda: {"inj": 0, "det": 0, "hung": 0}))
    for e in events:
        if e["type"] == "request":
            c = req[e["test"]][e["round"]]
            c["env"] += 1
            c["corr"] += e["correct"]
        elif e["type"] == "fault":
            c = flt[e["test"]][e["round"]]
            c["inj"] += 1
            c["det"] += e["detected"]
            c["hung"] += e["hung"]

    def summary_t(per_round):
        env = sum(c["env"] for c in per_round.values())
        corr = sum(c["corr"] for c in per_round.values())
        m, d = mean_stdev([ratio(per_round[r]["corr"], per_round[r]["env"])
                           for r in rounds_run if r in per_round])
        return {"N_env": env, "N_corr": corr, "T": ratio(corr, env),
                "T_round_mean": m, "T_round_stdev": d,
                "per_round": {r: dict(c, T=ratio(c["corr"], c["env"]))
                              for r, c in sorted(per_round.items())}}

    def summary_c(per_round):
        inj = sum(c["inj"] for c in per_round.values())
        det = sum(c["det"] for c in per_round.values())
        hung = sum(c["hung"] for c in per_round.values())
        m, d = mean_stdev([ratio(per_round[r]["det"], per_round[r]["inj"])
                           for r in rounds_run if r in per_round])
        return {"N_inj": inj, "N_det": det, "C": ratio(det, inj),
                "undetected_corruption": inj - det - hung, "hangs": hung,
                "C_round_mean": m, "C_round_stdev": d,
                "per_round": {r: dict(c, C=ratio(c["det"], c["inj"]))
                              for r, c in sorted(per_round.items())}}

    def add_up(group):
        total = defaultdict(lambda: defaultdict(int))
        for per_round in group.values():
            for r, c in per_round.items():
                for k, v in c.items():
                    total[r][k] += v
        return total

    return {
        "functional": {
            "per_test": {t: summary_t(r) for t, r in sorted(req.items())},
            "total": summary_t(add_up(req)) if req else None,
        },
        "fault_injection": {
            "per_test": {t: summary_c(r) for t, r in sorted(flt.items())},
            "total": summary_c(add_up(flt)) if flt else None,
        },
    }


def pct(v):
    return "   -   " if v is None else f"{100 * v:6.2f}%"


def print_summary(metrics, runs):
    print("\n" + "=" * 78)
    print(f"SUMMARY — {len(runs)} round(s)")
    print("=" * 78)

    print("\npytest results per round:")
    for run in runs:
        t = run["tests"]
        extra = "  [TIMEOUT]" if run["timeout"] else ""
        print(f"  Round {run['round']:3d}: {t['passed']} passed, {t['failed']} failed, "
              f"{t['skipped']} skipped  ({run['duration_s']:.0f}s){extra}")
        for name in t["failures"]:
            print(f"      ✗ {name}")

    def mean_txt(m, d):
        return "" if m is None else f"   per-round mean {pct(m)} ± {100 * d:.2f}"

    print("\nFunctional campaign — T = N_corr / N_env")
    print(f"  {'Test':32s} {'N_env':>7s} {'N_corr':>7s} {'T':>8s}")
    func = metrics["functional"]
    for name, r in func["per_test"].items():
        print(f"  {name:32s} {r['N_env']:7d} {r['N_corr']:7d} {pct(r['T'])}"
              f"{mean_txt(r['T_round_mean'], r['T_round_stdev'])}")
    if func["total"]:
        r = func["total"]
        print(f"  {'TOTAL':32s} {r['N_env']:7d} {r['N_corr']:7d} {pct(r['T'])}")
    else:
        print("  (no requests recorded)")

    print("\nFault injection — C = N_det / N_inj")
    print(f"  {'Test':32s} {'N_inj':>6s} {'N_det':>6s} {'corrupt':>8s} {'hung':>6s} {'C':>8s}")
    inj = metrics["fault_injection"]
    for name, r in inj["per_test"].items():
        print(f"  {name:32s} {r['N_inj']:6d} {r['N_det']:6d} {r['undetected_corruption']:8d} "
              f"{r['hangs']:6d} {pct(r['C'])}"
              f"{mean_txt(r['C_round_mean'], r['C_round_stdev'])}")
    if inj["total"]:
        r = inj["total"]
        print(f"  {'TOTAL':32s} {r['N_inj']:6d} {r['N_det']:6d} {r['undetected_corruption']:8d} "
              f"{r['hangs']:6d} {pct(r['C'])}")
    else:
        print("  (no injected faults recorded)")
    print("  corrupt = undetected without a hang (silent corruption); "
          "hung = system did not respond after the fault")


def main():
    args = parse_args()
    output = args.output or (ROOT / "results" / time.strftime("%Y%m%d_%H%M%S"))
    output.mkdir(parents=True, exist_ok=True)
    metrics_file = output / "metrics.jsonl"

    print(f"Rounds: {args.rounds} | Suites: {', '.join(args.suites)} | Output: {output}")
    if "bootloader" in args.suites and not (args.with_power and args.with_update):
        skipped = [name for name, enabled in [("power-loss tests", args.with_power),
                                              ("test_full_update_process", args.with_update)]
                   if not enabled]
        print("Bootloader: skipping " + ", ".join(skipped))

    runs = []
    try:
        for round_no in range(1, args.rounds + 1):
            print("\n" + "#" * 78 + f"\n# ROUND {round_no}/{args.rounds}\n" + "#" * 78)
            runs.append(run_round(round_no, args, output, metrics_file))
            if round_no < args.rounds:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nInterrupted by user — computing metrics for the completed rounds.")

    metrics = compute_metrics(metrics_file, [r["round"] for r in runs])
    with open(output / "summary.json", "w") as f:
        json.dump({"arguments": {k: (str(v) if isinstance(v, Path) else v)
                                 for k, v in vars(args).items()},
                   "runs": runs, "metrics": metrics},
                  f, indent=2, ensure_ascii=False)

    print_summary(metrics, runs)
    print(f"\nFiles in: {output}")


if __name__ == "__main__":
    main()
