"""Execution context shared by named application operations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from openkb.cancellation import cancellation_scope
from openkb.config import LlmCredentialBundle
from openkb.config_state import ConfigSnapshot, capture_config
from openkb.locks import LockCancelled


@dataclass
class ExecutionContext:
    snapshot: ConfigSnapshot | None = field(default=None, repr=False)
    cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)
    on_event: Callable[[dict], None] = field(default=lambda event: None, repr=False)
    on_snapshot: Callable[[ConfigSnapshot], None] = field(default=lambda value: None, repr=False)
    install_process_settings: bool = False
    _announced: bool = field(default=False, init=False, repr=False)

    def check_stop(self) -> None:
        if self.cancelled():
            raise LockCancelled("Stopped before starting the next operation")

    def waiting(self) -> None:
        self.on_event({"stage": "waiting"})

    @contextmanager
    def begin(self, kb_dir: Path) -> Iterator[LlmCredentialBundle]:
        self.check_stop()
        if self.snapshot is None:
            self.snapshot = capture_config(kb_dir, cancelled=self.cancelled, on_wait=self.waiting)
            # The worker's control channel acknowledges this before any
            # business work, so later batch units cannot silently re-resolve.
        if not self._announced:
            self.on_snapshot(self.snapshot)
            self._announced = True
        if self.snapshot.kb_dir != str(kb_dir.resolve()):
            raise ValueError("Execution context belongs to another knowledge base")
        if self.install_process_settings:
            self.snapshot.install_worker_environment()
        with self.snapshot.activate(), cancellation_scope(self.cancelled):
            self.check_stop()
            yield LlmCredentialBundle(**self.snapshot.values()["credentials"])
