"""Short local I/O off the Qt thread, with cancellable lock waits."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from openkb.locks import kb_lock


class LocalIO(QObject):
    completed = Signal(int, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="openkb-read")
        self._stop = threading.Event()
        self._sequence = 0
        self._callbacks = {}
        self._futures = []
        self.completed.connect(self._deliver)

    def submit(self, operation, callback, *, kb: Path | None = None, exclusive=False):
        self._sequence += 1
        sequence = self._sequence
        self._callbacks[sequence] = callback

        def run():
            if kb is not None:
                if not (kb / ".openkb/config.yaml").is_file():
                    raise ValueError("请选择已有的知识库目录")
                with kb_lock(kb / ".openkb", exclusive=exclusive, cancelled=self._stop.is_set):
                    return operation()
            return operation()

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
        callback = self._callbacks.pop(sequence, None)
        if callback and not self._stop.is_set():
            callback(value, error)

    def stop(self):
        self._stop.set()
        self._callbacks.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def stopped(self):
        return all(future.done() for future in self._futures)
