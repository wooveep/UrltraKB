"""Single executors inside the existing KB lease; consumers can stop waiting independently."""

import threading
import time
from contextlib import contextmanager

from openkb.execution_measurement import measure_span
from openkb.processing import ProcessingIncomplete, processing_checkpoint, processing_wait_limit

LOCK = threading.RLock()
_PENDING: dict[tuple[str, str], threading.Event] = {}


class Flight:
    def __init__(self, key, event, owner):
        self.key, self.event, self.owner = key, event, owner

    def wait(self, stage):
        with measure_span("analysis_wait"):
            self._wait(stage)

    def _wait(self, stage):
        deadline = time.monotonic() + processing_wait_limit()
        while not self.event.wait(0.05):
            processing_checkpoint(stage)
            if time.monotonic() >= deadline:
                raise ProcessingIncomplete("analysis_wait_timeout", stage)
        processing_checkpoint(stage)

    def finish(self):
        if self.owner:
            with LOCK:
                if _PENDING.get(self.key) is self.event:
                    del _PENDING[self.key]
                self.event.set()


def claim(key):
    with LOCK:
        if key in _PENDING:
            return Flight(key, _PENDING[key], False)
        event = _PENDING[key] = threading.Event()
        return Flight(key, event, True)


@contextmanager
def claimed(analysis, payloads):
    flights = []
    try:
        for payload in payloads:
            flights.append(analysis.claim(payload))
        yield flights
    finally:
        for flight in flights:
            if flight is not None and flight.owner:
                flight.finish()
