"""Native subscription behavior against real changing directories and task receipts."""

import threading
import time
from types import SimpleNamespace

from openkb.runtime.watch import NativeWatch
from openkb.state import HashRegistry


class TaskSink:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def submit(self, root, requests):
        with self.lock:
            key = str(len(self.items))
            self.items[key] = (
                requests[0],
                SimpleNamespace(state="queued", results=(), processes_reaped=True),
            )
            return key

    def get(self, key):
        with self.lock:
            return self.items[key][1]

    def finish(self, key, revision=None, state="completed"):
        with self.lock:
            self.items[key][1].state = state
            self.items[key][1].results = (SimpleNamespace(revision=revision),)

    def release_input_wait(self, key):
        with self.lock:
            view = self.items[key][1]
            if view.state != "waiting" or view.stage != "waiting-input":
                return False
            view.state = "stopped"
            return True


def eventually(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    assert condition()


def test_startup_scan_deduplicates_and_observes_recursive_atomic_moves(kb_dir):
    raw = kb_dir / "raw"
    known = raw / "known.md"
    known.write_text("known")
    registry = HashRegistry(kb_dir / ".openkb/hashes.json")
    registry.add(registry.hash_file(known), {"name": known.name})
    folder = raw / "中文 目录"
    folder.mkdir()
    note = folder / "first.md"
    note.write_text("new")
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.08, scan_interval=0.03)
    try:
        eventually(lambda: len(sink.items) == 1)
        assert sink.items["0"][0].source == str(note)
        assert watch.view().startup_found == 2
        temporary = folder / ".temporary"
        temporary.write_text("atomic")
        temporary.rename(folder / "second.md")
        eventually(lambda: len(sink.items) == 2)
    finally:
        watch.stop()
        assert watch.join(5)
    (folder / "late.md").write_text("late")
    time.sleep(0.15)
    assert len(sink.items) == 2 and watch.view().state == "stopped"
    assert sink.get("0").state == "queued"  # Stopping subscription does not stop accepted tasks.


def test_continuous_file_does_not_delay_an_independent_stable_input(kb_dir):
    noisy = kb_dir / "raw/noisy.md"
    noisy.write_text("changing")
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.15, scan_interval=0.02)
    try:
        stable = kb_dir / "raw/stable.md"
        stable.write_text("stable")
        for index in range(20):
            noisy.write_text(str(index))
            time.sleep(0.025)
        assert [item[0].source for item in sink.items.values()] == [str(stable)]
        eventually(lambda: len(sink.items) == 2)
    finally:
        watch.stop()
        assert watch.join(5)


def test_changes_coalesce_behind_active_task_and_failed_processed_version_is_not_replayed(kb_dir):
    path = kb_dir / "raw/note.md"
    path.write_text("first")
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.05, scan_interval=0.02)
    try:
        eventually(lambda: len(sink.items) == 1)
        path.write_text("second")
        path.write_text("latest")
        time.sleep(0.2)
        assert len(sink.items) == 1
        # The worker refreshed to this version before business. Its failure
        # does not authorize the scanner to replay that same full operation.
        sink.finish("0", HashRegistry.hash_file(path), state="failed")
        time.sleep(0.25)
        assert len(sink.items) == 1
        path.write_text("a later version")
        eventually(lambda: len(sink.items) == 2)
    finally:
        watch.stop()
        assert watch.join(5)


def test_bounded_candidates_rescan_overflow_without_losing_files(kb_dir):
    for index in range(20):
        (kb_dir / "raw" / f"note-{index}.md").write_text(str(index))
    sink = TaskSink()
    watch = NativeWatch(
        kb_dir, sink, debounce=0.06, scan_interval=0.02, capacity=3, max_submitted=2
    )
    try:
        deadline = time.monotonic() + 8
        while len(sink.items) < 20 and time.monotonic() < deadline:
            for key in list(sink.items):
                sink.finish(key)
            assert watch.view().pending <= 3
            time.sleep(0.02)
        assert len(sink.items) == 20
        assert len({item[0].source for item in sink.items.values()}) == 20
        assert watch.view().overflow_scans > 0
    finally:
        watch.stop()
        assert watch.join(5)


def test_unreadable_subdirectory_does_not_stop_other_files_or_later_rescan(kb_dir):
    import os

    import pytest

    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("POSIX permission fixture requires a non-root user")
    folder = kb_dir / "raw/restricted"
    folder.mkdir()
    (folder / "later.md").write_text("later")
    (kb_dir / "raw/available.md").write_text("available")
    folder.chmod(0)
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.05, scan_interval=0.02)
    try:
        eventually(lambda: len(sink.items) == 1)
        assert watch.view().state == "watching"
        assert "restricted" in (watch.view().error or "")
        folder.chmod(0o700)
        eventually(lambda: len(sink.items) == 2)
    finally:
        folder.chmod(0o700)
        watch.stop()
        assert watch.join(5)


def test_unknown_worker_result_pauses_submission_until_user_inspects(kb_dir):
    path = kb_dir / "raw/unknown.md"
    path.write_text("first version")
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.05, scan_interval=0.02)
    try:
        eventually(lambda: len(sink.items) == 1)
        path.write_text("version possibly consumed by the interrupted worker")
        sink.finish("0", state="interrupted")
        eventually(lambda: watch.view().state == "blocked")
        time.sleep(0.15)
        assert len(sink.items) == 1
    finally:
        watch.stop()
        assert watch.join(5)


def test_input_wait_returns_to_candidates_so_independent_file_can_start(kb_dir):
    noisy = kb_dir / "raw/noisy.md"
    noisy.write_text("first")
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.05, scan_interval=0.02, max_submitted=1)
    try:
        eventually(lambda: len(sink.items) == 1)
        waiting = sink.get("0")
        waiting.state, waiting.stage = "waiting", "waiting-input"
        stable = kb_dir / "raw/stable.md"
        stable.write_text("independent")
        eventually(lambda: len(sink.items) == 2)
        assert sink.items["1"][0].source == str(stable)
        assert waiting.state == "stopped"
        assert watch.view().active == 1
    finally:
        watch.stop()
        assert watch.join(5)


def test_full_admission_window_does_not_reread_unchanged_waiting_bytes(kb_dir, monkeypatch):
    first = kb_dir / "raw/first.md"
    first.write_text("first")
    sink = TaskSink()
    original = HashRegistry.hash_file
    reads = []

    def observed(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(HashRegistry, "hash_file", observed)
    watch = NativeWatch(kb_dir, sink, debounce=0.05, scan_interval=0.02, max_submitted=1)
    try:
        eventually(lambda: len(sink.items) == 1)
        waiting = kb_dir / "raw/waiting.md"
        waiting.write_text("large stable input")
        eventually(lambda: waiting in reads)
        time.sleep(0.25)
        assert reads.count(waiting) == 1
    finally:
        watch.stop()
        assert watch.join(5)


def test_stop_during_candidate_hash_does_not_withdraw_already_submitted_work(kb_dir, monkeypatch):
    first = kb_dir / "raw/first.md"
    first.write_text("first")
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.05, scan_interval=0.02, max_submitted=1)
    hashing, release = threading.Event(), threading.Event()
    original = HashRegistry.hash_file
    stable = kb_dir / "raw/stable.md"

    def paused(path):
        if path == stable:
            hashing.set()
            assert release.wait(5)
        return original(path)

    try:
        eventually(lambda: len(sink.items) == 1)
        waiting = sink.get("0")
        waiting.state, waiting.stage = "waiting", "waiting-input"
        monkeypatch.setattr(HashRegistry, "hash_file", paused)
        stable.write_text("second")
        assert hashing.wait(5)
        watch.stop()
        release.set()
        assert watch.join(5)
        assert waiting.state == "waiting" and len(sink.items) == 1
    finally:
        release.set()
        watch.stop()
        assert watch.join(5)
