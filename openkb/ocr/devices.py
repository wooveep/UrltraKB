"""Bounded cross-process GPU admission; held until the supervised process is reaped."""

import time
from contextlib import contextmanager

import portalocker

from openkb import config
from openkb.processing import ProcessingIncomplete, processing_checkpoint


@contextmanager
def device_slot(device, seconds, started):
    if device == "cpu":
        yield
        return
    # Conservative first release: all local GPU models share one inference slot.
    # It also prevents two simultaneous probes from loading competing models.
    path = config.GLOBAL_CONFIG_DIR / "ocr-gpu.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = portalocker.Lock(str(path), timeout=0)
    while True:
        processing_checkpoint("ocr")
        if time.monotonic() - started >= seconds:
            raise ProcessingIncomplete("ocr_time_budget_exhausted", "ocr")
        try:
            lock.acquire()
            break
        except portalocker.exceptions.LockException:
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.release()
