"""Short local I/O off the Qt thread, with cancellable lock waits."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from openkb.locks import kb_lock, kb_repair_lock


class _WaitingForKB(Exception):
    """No operation began; release the thread while another process owns the KB."""


def _defer_wait():
    raise _WaitingForKB()


class LocalIO(QObject):
    completed = Signal(int, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="openkb-read")
        self._stop = threading.Event()
        self._sequence = 0
        self._callbacks = {}
        self._operations = {}
        self._futures = []
        self.completed.connect(self._deliver)

    def submit(
        self,
        operation,
        callback,
        *,
        kb: Path | None = None,
        exclusive=False,
        global_settings=False,
        creating=False,
        repair=False,
        obsolete=lambda: False,
    ):
        if self._stop.is_set():
            return
        self._sequence += 1
        sequence = self._sequence
        self._callbacks[sequence] = callback

        def run():
            if self._stop.is_set() or obsolete():
                return None
            with ExitStack() as scope:
                wait_options = {
                    "cancelled": lambda: self._stop.is_set() or obsolete(),
                    "on_wait": _defer_wait,
                }
                if kb is not None:
                    if not creating and not (
                        (kb / ".openkb").is_dir()
                        if repair
                        else (kb / ".openkb/config.yaml").is_file()
                    ):
                        raise ValueError("请选择已有的知识库目录")
                    lease = (
                        kb_repair_lock(kb / ".openkb", **wait_options)
                        if repair
                        else kb_lock(kb / ".openkb", exclusive=exclusive, **wait_options)
                    )
                    scope.enter_context(lease)
                if global_settings:
                    from openkb.config import _with_global_config_lock

                    scope.enter_context(
                        _with_global_config_lock(
                            recover=not repair or kb is not None, **wait_options
                        )
                    )
                return operation()

        self._operations[sequence] = (run, obsolete)
        self._attempt(sequence)

    def _attempt(self, sequence):
        if sequence not in self._operations or self._stop.is_set():
            return
        run, obsolete = self._operations[sequence]
        if obsolete():
            self._operations.pop(sequence, None)
            self._callbacks.pop(sequence, None)
            return
        future = self._pool.submit(run)
        self._futures = [f for f in self._futures if not f.done()]
        self._futures.append(future)

        def done(result):
            if result.cancelled() or self._stop.is_set():
                return
            try:
                self.completed.emit(sequence, result.result(), None)
            except Exception as exc:
                self.completed.emit(sequence, None, exc)

        future.add_done_callback(done)

    def _deliver(self, sequence, value, error):
        if isinstance(error, _WaitingForKB) and not self._stop.is_set():
            QTimer.singleShot(200, lambda: self._attempt(sequence))
            return
        operation = self._operations.pop(sequence, None)
        callback = self._callbacks.pop(sequence, None)
        if callback and operation and not operation[1]() and not self._stop.is_set():
            callback(value, error)

    def stop(self):
        self._stop.set()
        self._callbacks.clear()
        self._operations.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def stopped(self):
        return all(future.done() for future in self._futures)
