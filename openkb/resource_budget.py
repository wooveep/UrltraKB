"""Shared admission and byte-bounded immutable range cache for document work."""

import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

from openkb.log import logger
from openkb.processing import ProcessingIncomplete
from openkb.resource_memory import memory_sample

_CURRENT = ContextVar("document_resources", default=None)
CACHE_BYTES = 64 * 1024**2


class ResourceBudget:
    def __init__(self):
        sample = memory_sample()
        self.limit = min(sample["available"] // 2, 2 * 1024**3) if sample else None
        from openkb.runtime.family_budget import current_family

        if self.limit is not None and (family := current_family()):
            self.limit = family.memory_limit(self.limit)
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.lock = threading.RLock()
        self.inputs = {}

    def reserve_input(self, owner, key, size, stage):
        from openkb.resource_checks import CONTRACT_BYTES

        with self.lock:
            identity = (owner, key)
            if sum(self.inputs.values()) - self.inputs.get(identity, 0) + size > CONTRACT_BYTES:
                raise ProcessingIncomplete("resource_input_exceeds_budget", stage)
            self.inputs[identity] = size

    def release_input(self, owner, key):
        with self.lock:
            self.inputs.pop((owner, key), None)

    def admit(self, expected_bytes=0, *, stage, active=False):
        sample = memory_sample()
        if sample is None or self.limit is None:
            return True
        occupied = max(sample["resident"], sample["private"] or 0)
        if occupied + expected_bytes < self.limit * 0.8:
            return True
        with self.lock:
            self.cache.clear()
            self.cache_bytes = 0
        if active:
            return False  # Drain the bounded active work before trying admission again.
        sample = memory_sample()
        occupied = max(sample["resident"], sample["private"] or 0)
        if occupied + expected_bytes > self.limit:
            logger.warning(
                "Resource limit stage=%s memory=%s expected_bytes=%s budget_bytes=%s",
                stage,
                sample,
                expected_bytes,
                self.limit,
            )
            raise ProcessingIncomplete("resource_memory_insufficient", stage)
        return True  # At the soft threshold a single batch may make progress.

    def read(self, key, load):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
        value = load()
        # A Python string uses up to four bytes per code point, plus its header.
        size = value.__sizeof__()
        if size <= CACHE_BYTES:
            with self.lock:
                if key in self.cache:
                    return self.cache[key]
                while self.cache and self.cache_bytes + size > CACHE_BYTES:
                    _, old = self.cache.popitem(last=False)
                    self.cache_bytes -= old.__sizeof__()
                self.cache[key] = value
                self.cache_bytes += size
        return value


def current_resources():
    return _CURRENT.get()


def check_memory(expected_bytes=0, *, stage):
    if current := _CURRENT.get():
        current.admit(expected_bytes, stage=stage)


@contextmanager
def resource_scope():
    if _CURRENT.get() is not None:
        yield _CURRENT.get()
        return
    budget = ResourceBudget()
    token = _CURRENT.set(budget)
    try:
        yield budget
    finally:
        budget.cache.clear()
        _CURRENT.reset(token)
