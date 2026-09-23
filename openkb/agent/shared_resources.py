"""Shared resource admission and bounded concurrency for compilation stages."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator


class SharedResourcePool:
    """Bounded concurrency (2..4) and atomic inflight budget across compilation stages."""

    def __init__(self, concurrency: int = 2, max_inflight_tokens: int | None = None):
        # Starts at 2, capped at 4, respecting user stricter configuration
        self.concurrency = max(1, min(4, concurrency))
        self.semaphore = threading.BoundedSemaphore(self.concurrency)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.current_inflight_tokens = 0
        self.peak_inflight_tokens = 0
        self.max_inflight_tokens = max_inflight_tokens
        self.page_locks: dict[str, threading.Lock] = {}

    def get_page_lock(self, page_path: str) -> threading.Lock:
        with self.lock:
            return self.page_locks.setdefault(page_path, threading.Lock())

    @contextmanager
    def admit(
        self,
        estimated_tokens: int = 0,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        """Reserve one physical dispatch slot without outliving its request.

        ``ExecutionBudget`` supplies a request-local checkpoint while a model
        call is waiting here.  The pool deliberately does not own another
        timeout policy: it observes that request's actual deadline, cancellation
        latch, and document allowance before each bounded wait and immediately
        after admission.  Direct users retain the ordinary processing
        checkpoint, which still observes cancellation.
        """

        if checkpoint is None:
            from openkb.processing import processing_checkpoint

            checkpoint = processing_checkpoint

        while True:
            checkpoint()
            if self.semaphore.acquire(timeout=0.05):
                break
        reserved = False
        try:
            with self.condition:
                # A request which is legal on its own must still make progress.
                # The token gate only waits while another request owns capacity;
                # it is not a second, hidden model-context limit.
                while (
                    self.max_inflight_tokens is not None
                    and self.current_inflight_tokens
                    and self.current_inflight_tokens + estimated_tokens > self.max_inflight_tokens
                ):
                    checkpoint()
                    self.condition.wait(timeout=0.05)
                checkpoint()
                self.current_inflight_tokens += estimated_tokens
                self.peak_inflight_tokens = max(
                    self.peak_inflight_tokens, self.current_inflight_tokens
                )
                reserved = True
                current = self.current_inflight_tokens
            from openkb.execution_measurement import record_inflight_tokens

            record_inflight_tokens(current)
            # Capacity can be freed just as an outer request expires.  Check
            # once more before exposing the transport to its caller.
            checkpoint()
            yield
        finally:
            if reserved:
                with self.condition:
                    self.current_inflight_tokens = max(
                        0, self.current_inflight_tokens - estimated_tokens
                    )
                    self.condition.notify_all()
            self.semaphore.release()
