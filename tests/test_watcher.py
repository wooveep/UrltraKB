"""Tests for openkb.watcher (Task 12)."""

from __future__ import annotations

from unittest.mock import MagicMock

from openkb.watcher import DebouncedHandler, start_watch


def _make_file_event(src_path: str, is_directory: bool = False):
    """Create a mock watchdog file event."""
    event = MagicMock()
    event.src_path = src_path
    event.is_directory = is_directory
    return event


class TestDebouncedHandler:
    def test_collects_created_files(self):
        callback = MagicMock()
        handler = DebouncedHandler(callback, debounce_seconds=100)

        handler.on_created(_make_file_event("/raw/doc.pdf"))
        handler.on_created(_make_file_event("/raw/notes.md"))

        # Cancel pending timer; check pending set
        if handler._timer:
            handler._timer.cancel()

        assert "/raw/doc.pdf" in handler._pending
        assert "/raw/notes.md" in handler._pending

    def test_collects_modified_files(self):
        callback = MagicMock()
        handler = DebouncedHandler(callback, debounce_seconds=100)

        handler.on_modified(_make_file_event("/raw/paper.txt"))

        if handler._timer:
            handler._timer.cancel()

        assert "/raw/paper.txt" in handler._pending

    def test_ignores_directories(self):
        callback = MagicMock()
        handler = DebouncedHandler(callback, debounce_seconds=100)

        handler.on_created(_make_file_event("/raw/subdir", is_directory=True))

        if handler._timer:
            handler._timer.cancel()

        assert len(handler._pending) == 0

    def test_ignores_hidden_files(self):
        callback = MagicMock()
        handler = DebouncedHandler(callback, debounce_seconds=100)

        handler.on_created(_make_file_event("/raw/.hidden_file"))
        handler.on_created(_make_file_event("/raw/.DS_Store"))

        if handler._timer:
            handler._timer.cancel()

        assert len(handler._pending) == 0

    def test_flush_calls_callback_with_sorted_paths(self):
        received = []

        def callback(paths):
            received.extend(paths)

        handler = DebouncedHandler(callback, debounce_seconds=100)
        handler._pending = {"/raw/b.pdf", "/raw/a.md", "/raw/c.txt"}

        handler._flush()

        assert received == ["/raw/a.md", "/raw/b.pdf", "/raw/c.txt"]

    def test_flush_clears_pending(self):
        callback = MagicMock()
        handler = DebouncedHandler(callback, debounce_seconds=100)
        handler._pending = {"/raw/doc.pdf"}

        handler._flush()

        assert len(handler._pending) == 0

    def test_flush_does_not_call_callback_when_empty(self):
        callback = MagicMock()
        handler = DebouncedHandler(callback, debounce_seconds=100)

        handler._flush()

        callback.assert_not_called()

    def test_debounce_resets_timer_on_new_event(self):
        callback = MagicMock()
        handler = DebouncedHandler(callback, debounce_seconds=100)

        handler.on_created(_make_file_event("/raw/a.pdf"))
        first_timer = handler._timer

        handler.on_created(_make_file_event("/raw/b.pdf"))
        second_timer = handler._timer

        # Timer should have been replaced
        assert first_timer is not second_timer

        if handler._timer:
            handler._timer.cancel()

    def test_mixed_events_collected(self):
        callback = MagicMock()
        handler = DebouncedHandler(callback, debounce_seconds=100)

        handler.on_created(_make_file_event("/raw/new.pdf"))
        handler.on_modified(_make_file_event("/raw/existing.md"))
        handler.on_created(_make_file_event("/raw/.hidden"))  # should be ignored
        handler.on_created(_make_file_event("/raw/subdir", is_directory=True))  # ignored

        if handler._timer:
            handler._timer.cancel()

        assert handler._pending == {"/raw/new.pdf", "/raw/existing.md"}


def test_atomic_move_uses_destination_and_stop_prevents_late_delivery(tmp_path):
    import threading

    from watchdog.events import FileMovedEvent

    delivered = threading.Event()
    received = []

    def callback(paths):
        received.extend(paths)
        delivered.set()

    handler = DebouncedHandler(callback, debounce_seconds=0.03)
    handler.on_moved(FileMovedEvent(str(tmp_path / ".temporary"), str(tmp_path / "final.md")))
    assert delivered.wait(2)
    assert received == [str(tmp_path / "final.md")]
    handler.stop()
    handler.on_created(_make_file_event(str(tmp_path / "late.md")))
    assert handler.join(2)
    assert received == [str(tmp_path / "final.md")]


def test_stopped_observer_reaps_pending_debounce_timer(tmp_path):
    import time

    received = []
    watcher = start_watch(tmp_path, received.extend, debounce=1)
    (tmp_path / "file.md").write_text("complete")
    time.sleep(0.1)
    watcher.stop()
    watcher.join(2)
    assert not watcher.is_alive()
    time.sleep(0.05)
    assert received == []


def test_blocking_watch_cancellation_reaps_observer_and_callbacks(tmp_path):
    import threading
    import time

    from openkb.watcher import watch_directory

    delivered = threading.Event()
    cancelled = threading.Event()
    errors = []
    received = []

    def callback(paths):
        received.extend(paths)
        delivered.set()

    def run():
        try:
            watch_directory(tmp_path, callback, debounce=0.03, cancelled=cancelled.is_set)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not delivered.is_set() and time.monotonic() < deadline:
            (tmp_path / "first.md").write_text("ready")
            delivered.wait(0.1)
        assert delivered.is_set(), errors
    finally:
        cancelled.set()
        thread.join(3)
    assert not thread.is_alive()
    assert not errors
    previous = list(received)
    (tmp_path / "late.md").write_text("after stop")
    assert received == previous
