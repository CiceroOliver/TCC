"""
Evaluation metrics recorded by the tests.

- Correct-response rate: T = N_corr / N_env
- Detection coverage:    C = N_det / N_inj

Each event is appended as one JSON line to the file named by the NASCERR_METRICS
environment variable (set by tests/run_rounds.py). Without it, nothing is
recorded and the tests run normally.
"""

import json
import os
import time

FILE_ENV = "NASCERR_METRICS"
ROUND_ENV = "NASCERR_ROUND"


def _write(event: dict):
    path = os.environ.get(FILE_ENV)
    if not path:
        return
    event["round"] = int(os.environ.get(ROUND_ENV, "0"))
    event["t"] = time.time()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def request(test: str, correct: bool, detail: str = "") -> bool:
    """Record one request sent to the device and whether its response was correct."""
    _write({"type": "request", "test": test, "correct": bool(correct), "detail": detail})
    return bool(correct)


def measure(test: str, fn, validate=None, detail: str = ""):
    """
    Call fn() and record it as a request.

    The response is correct when fn() does not raise, returns a dict without
    "error" and, if given, validate(response) is true. The response is returned
    unchanged; exceptions are recorded as incorrect and re-raised.
    """
    try:
        response = fn()
    except Exception as e:
        request(test, False, f"{detail} | exception: {e}")
        raise

    correct = isinstance(response, dict) and not response.get("error")
    if correct and validate is not None:
        try:
            correct = bool(validate(response))
        except Exception:
            correct = False
    request(test, correct, detail)
    return response


def fault(test: str, detected: bool, hung: bool = False, detail: str = "") -> bool:
    """
    Record one injected fault.

    detected: the system detected and handled the fault.
    hung:     the system stopped responding after the fault.
    Not detected and not hung means silent corruption.
    """
    _write({"type": "fault", "test": test, "detected": bool(detected),
            "hung": bool(hung), "detail": detail})
    return bool(detected)


class MeasuredOperation:
    """
    Wraps an Operation and records each call to the methods listed in
    `validators` ({method_name: validation function or None}) as a request.
    Other attributes pass through unrecorded. Use `.raw` for calls that must
    not count towards the metric.
    """

    def __init__(self, operation, test: str, validators: dict):
        self.raw = operation
        self._test = test
        self._validators = validators

    def __getattr__(self, name):
        attr = getattr(self.raw, name)
        if name not in self._validators or not callable(attr):
            return attr
        validate = self._validators[name]

        def call(*args, **kwargs):
            return measure(self._test, lambda: attr(*args, **kwargs), validate, name)

        return call
