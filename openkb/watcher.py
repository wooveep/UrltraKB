"""File-system watcher for the OpenKB raw/ directory.

Watches for new or modified files and debounces rapid bursts of events
before calling the user's callback with a sorted list of affected paths.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class DebouncedHandler(FileSystemEventHandler):
    """Debounced file-system event handler.

    Collects file creation/modification events and waits *debounce_seconds*
    after the last event before calling *callback* with all pending paths.
    Directories and dotfiles (hidden files) are ignored.

    Args:
        callback: Called with a sorted list of path strings when the debounce
            timer fires.
        debounce_seconds: How long to wait after the last event before
            flushing. Defaults to 2.0 seconds.
    """

    def __init__(
        self, callback: Callable[[list[str]], None], debounce_seconds: float = 2.0
    ) -> None:
        super().__init__()
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None
        self._lock = threading.Condition()
        self._accepting = True
        self._callbacks = 0
        self._timers: set[threading.Timer] = set()

    def _schedule_flush(self) -> None:
        """Cancel any existing timer and start a fresh debounce timer."""
        with self._lock:
            if not self._accepting:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._flush)
            self._timer.daemon = True
            self._timers = {timer for timer in self._timers if timer.is_alive()}
            self._timers.add(self._timer)
            self._timer.start()

    def _flush(self) -> None:
        """Call the callback with all collected pending paths, then clear."""
        with self._lock:
            if not self._accepting:
                return
            paths = sorted(self._pending)
            self._pending.clear()
            self._timer = None
            self._callbacks += bool(paths)
        try:
            if paths:
                self._callback(paths)
        finally:
            with self._lock:
                self._callbacks -= bool(paths)
                self._lock.notify_all()

    def _handle_event(self, event) -> None:
        """Add the event's source path to pending if it's a supported file."""
        if event.is_directory:
            return
        path = Path(event.src_path)
        # Ignore hidden/dotfiles
        if path.name.startswith("."):
            return
        with self._lock:
            if not self._accepting:
                return
            self._pending.add(str(path))
        self._schedule_flush()

    def on_created(self, event) -> None:
        """Handle file creation events."""
        self._handle_event(event)

    def on_modified(self, event) -> None:
        """Handle file modification events."""
        self._handle_event(event)

    def on_moved(self, event) -> None:
        from watchdog.events import FileCreatedEvent

        if not event.is_directory:
            self._handle_event(FileCreatedEvent(event.dest_path))

    def stop(self) -> None:
        with self._lock:
            self._accepting = False
            self._pending.clear()
            for timer in self._timers:
                timer.cancel()
            self._lock.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            timers = tuple(self._timers)
        for timer in timers:
            if timer is not threading.current_thread():
                timer.join(None if deadline is None else max(0, deadline - time.monotonic()))
        with self._lock:
            while self._callbacks:
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                self._lock.wait(None if deadline is None else max(0, deadline - time.monotonic()))
        return not any(timer.is_alive() for timer in timers)

    def is_alive(self) -> bool:
        with self._lock:
            return bool(self._callbacks or any(timer.is_alive() for timer in self._timers))


class WatchHandle:
    """The observer and debounce callbacks form one owned lifecycle."""

    def __init__(self, observer, handler: DebouncedHandler):
        self.observer, self.handler = observer, handler

    def stop(self) -> None:
        self.handler.stop()
        self.observer.stop()

    def join(self, timeout: float | None = None) -> None:
        started = time.monotonic()
        self.observer.join(timeout)
        self.handler.join(
            None if timeout is None else max(0, timeout - (time.monotonic() - started))
        )

    def is_alive(self) -> bool:
        return self.observer.is_alive() or self.handler.is_alive()


def watch_directory(
    raw_dir: Path,
    callback: Callable[[list[str]], None],
    debounce: float = 2.0,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Start watching *raw_dir* and block until Ctrl+C.

    Thin blocking wrapper around :func:`start_watch`; kept so the CLI
    ``openkb watch`` command is unchanged. The REST layer uses
    :func:`start_watch` directly so it can own the Observer's lifecycle.

    Args:
        raw_dir: Directory to watch for file changes.
        callback: Called with sorted list of new/modified file paths.
        debounce: Debounce delay in seconds. Defaults to 2.0.
    """
    observer = start_watch(raw_dir, callback, debounce)
    try:
        while observer.is_alive() and not (cancelled and cancelled()):
            observer.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


def start_watch(
    raw_dir: Path,
    callback: Callable[[list[str]], None],
    debounce: float = 2.0,
) -> WatchHandle:
    """Start a non-blocking watcher on *raw_dir* and return its Observer.

    The caller owns the Observer's lifecycle: call ``observer.stop()`` then
    ``observer.join()`` to tear it down. Debounce/filter behavior is identical
    to :func:`watch_directory`.

    Args:
        raw_dir: Directory to watch for file changes.
        callback: Called with sorted list of new/modified file paths.
        debounce: Debounce delay in seconds. Defaults to 2.0.

    Returns:
        The started watchdog Observer.
    """
    handler = DebouncedHandler(callback, debounce_seconds=debounce)
    observer = Observer()
    observer.schedule(handler, str(raw_dir), recursive=True)
    observer.start()
    return WatchHandle(observer, handler)
