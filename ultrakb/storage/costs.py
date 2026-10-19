"""Constant-space, local stage counters at existing Blob and transaction boundaries."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from time import perf_counter

from ultrakb.stage_observations import StorageCosts


class CostCapture:
    def __init__(self):
        self._guard, self._closed = Lock(), False
        self._values = {
            key: value
            for key, value in StorageCosts().model_dump().items()
            if type(value) in (int, float)
        }

    def transaction(self, began, acquired, ended, outcome):
        wait = (acquired if acquired is not None else ended) - began
        hold = ended - acquired if acquired is not None else 0.0
        with self._guard:
            if self._closed:
                return
            values = self._values
            values["writer_attempts"] += 1
            values["writer_acquired"] += int(acquired is not None)
            values[f"writer_{outcome}"] += 1
            values["writer_wait_seconds"] += wait
            values["writer_hold_seconds"] += hold
            values["writer_max_wait_seconds"] = max(values["writer_max_wait_seconds"], wait)
            values["writer_max_hold_seconds"] = max(values["writer_max_hold_seconds"], hold)

    def blob(self, operation, byte_count, began, failed):
        elapsed = perf_counter() - began
        with self._guard:
            if self._closed:
                return
            self._values[f"blob_{operation}_attempts"] += 1
            self._values[f"blob_{operation}_bytes"] += byte_count
            self._values[f"blob_{operation}_errors"] += int(failed)
            self._values[f"blob_{operation}_seconds"] += elapsed

    def dedup_hit(self):
        with self._guard:
            if not self._closed:
                self._values["blob_dedup_hits"] += 1

    def finish(self):
        with self._guard:
            self._closed = True
            return StorageCosts(**self._values)


_CAPTURE: ContextVar[CostCapture | None] = ContextVar("ultrakb_storage_costs", default=None)


def active_costs():
    return _CAPTURE.get()


@contextmanager
def capture_costs(enabled=True):
    if type(enabled) is not bool:
        raise ValueError("Storage-cost observation must be an explicit boolean")
    capture = CostCapture() if enabled else None
    token = _CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _CAPTURE.reset(token)
