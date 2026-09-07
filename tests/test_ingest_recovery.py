"""Failed recovery halts import work until explicitly repaired."""

import pytest


def test_import_does_not_retry_compilation_after_recovery_is_required(
    kb_dir, tmp_path, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.mutation import RecoveryRequired

    source = tmp_path / "note.md"
    source.write_text("# New document\n")
    calls = []

    async def fail(*args, **kwargs):
        calls.append(1)
        raise RecoveryRequired("Repair required during compilation")

    monkeypatch.setattr("openkb.agent.compiler.compile_short_doc", fail)
    with pytest.raises(RecoveryRequired):
        import_document(kb_dir, source)
    assert len(calls) == 1


def test_rest_watch_stops_processing_on_required_repair(kb_dir, monkeypatch):
    import threading

    from openkb.mutation import RecoveryRequired
    from openkb.watch_service import WatchRegistry

    started = threading.Event()
    calls = []

    async def fail_compile(doc_name, *args, **kwargs):
        calls.append(doc_name)
        started.set()
        raise RecoveryRequired("Explicit repair required")

    monkeypatch.setattr("openkb.agent.compiler.compile_short_doc", fail_compile)
    registry = WatchRegistry()
    state = registry.start("test", kb_dir, debounce=0.1)
    try:
        (kb_dir / "raw/bad.md").write_text("# Bad\n")
        (kb_dir / "raw/later.md").write_text("# Later\n")
        assert started.wait(5)
        state.worker_thread.join(5)
        assert not registry.status("test")["active"]
        assert calls == ["bad"]
        assert not (kb_dir / "wiki/sources/later.md").exists()
    finally:
        registry.stop_all()


@pytest.mark.parametrize("moment", ["prepare", "staging"])
def test_rest_watch_keeps_raw_input_identity_when_a_file_becomes_a_symlink(
    kb_dir, monkeypatch, moment
):
    import time
    from contextlib import contextmanager

    import openkb.application.documents as documents
    from openkb.state import HashRegistry
    from openkb.watch_service import WatchRegistry

    source = kb_dir / "raw/inbox/note.md"
    source.parent.mkdir()
    outside = kb_dir.with_name(kb_dir.name + "-outside.md")
    outside.write_text("# Outside\n")
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip("File symlinks unavailable")
    source.unlink()
    HashRegistry(kb_dir / ".openkb/hashes.json").add(
        "0" * 64, {"name": "note.md", "doc_name": "note", "path": "raw/other/note.md"}
    )
    staging, prepare = documents._staging_dir_for, documents.prepared_input
    compiled = []
    replaced = False

    def replace_input(path):
        nonlocal replaced
        if path == source and not replaced:
            source.unlink()
            source.symlink_to(outside)
            replaced = True

    def during_staging(root, path):
        result = staging(root, path)
        replace_input(path)
        return result

    @contextmanager
    def before_copy(path):
        replace_input(path)
        with prepare(path) as ready:
            yield ready

    async def compile_document(name, path, *args, **kwargs):
        compiled.append(path.read_text())

    monkeypatch.setattr("openkb.agent.compiler.compile_short_doc", compile_document)
    monkeypatch.setattr(
        documents,
        "prepared_input" if moment == "prepare" else "_staging_dir_for",
        before_copy if moment == "prepare" else during_staging,
    )
    registry = WatchRegistry()
    registry.start("guarded", kb_dir, debounce=0.1)
    try:
        source.write_text("# Original\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            counts = registry.status("guarded")["counters"]
            if counts["added"] + counts["failed"]:
                break
            time.sleep(0.01)
    finally:
        registry.stop_all()
    assert replaced
    assert compiled == (["# Original\n"] if moment == "staging" else [])
    entries = HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()
    assert all(item.get("path") != outside.as_posix() for item in entries.values())


@pytest.mark.parametrize("marker_failure", [False, True])
def test_failed_import_rollback_stays_blocked_after_the_io_error_is_gone(
    kb_dir, tmp_path, monkeypatch, marker_failure
):
    import openkb.mutation as mutation
    from openkb.application.documents import import_document
    from openkb.locks import atomic_write_text
    from openkb.mutation import RecoveryRequired, repair_marker

    source = tmp_path / "note.md"
    source.write_text("# New document\n")
    old = kb_dir / "wiki/sources/note.md"
    atomic_write_text(old, "# Previously retained source\n")
    copy = mutation._copy_file_atomic
    write_json = mutation.atomic_write_json

    def fail_marker(path, *args, **kwargs):
        if path == repair_marker(kb_dir):
            raise OSError("Repair marker cannot be written")
        return write_json(path, *args, **kwargs)

    def fail_restore(src, dest, **kwargs):
        if dest == old and "staging" in src.parts:
            raise OSError("Cannot restore source")
        return copy(src, dest, **kwargs)

    async def fail_compile(*args, **kwargs):
        raise ValueError("Compilation failed")

    with monkeypatch.context() as patch:
        patch.setattr("openkb.agent.compiler.compile_short_doc", fail_compile)
        patch.setattr("openkb.application.documents.time.sleep", lambda value: None)
        patch.setattr(mutation, "_copy_file_atomic", fail_restore)
        if marker_failure:
            patch.setattr(mutation, "atomic_write_json", fail_marker)
        with pytest.raises(RecoveryRequired):
            import_document(kb_dir, source)
    assert repair_marker(kb_dir).is_file() is not marker_failure
    assert list((kb_dir / ".openkb/journal").glob("*.json"))
    # Recoverable I/O on the next call must not silently clear the explicit repair gate.
    monkeypatch.setattr("openkb.agent.compiler.compile_short_doc", fail_compile)
    with pytest.raises(RecoveryRequired):
        import_document(kb_dir, source)
    from openkb.application.repair import repair_knowledge_base

    assert repair_knowledge_base(kb_dir).repaired
    assert old.read_text() == "# Previously retained source\n"
    assert not repair_marker(kb_dir).exists()


def test_native_watch_does_not_follow_a_replaced_knowledge_base(kb_dir):
    import shutil
    import time

    from openkb.runtime.tasks import TaskManager
    from openkb.runtime.watch import NativeWatch

    manager = TaskManager(history_dir=kb_dir.with_name(kb_dir.name + "-history"))
    watch = NativeWatch(kb_dir, manager, debounce=30, scan_interval=0.1)
    try:
        deadline = time.monotonic() + 5
        while watch.view().scans == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert watch.view().scans > 0
        retained = kb_dir.with_name(kb_dir.name + "-retained")
        kb_dir.rename(retained)
        shutil.copytree(retained, kb_dir)
        (kb_dir / "raw/new.md").write_text("# New collection\n")
        assert watch.join(2)
        assert watch.view().state == "failed"
        assert not (kb_dir / "wiki/sources/new.md").exists()
    finally:
        watch.stop()
        assert watch.join(5)
        manager.shutdown(stop=True)
        assert manager.join(5)


def test_stopping_rest_watch_cancels_a_start_waiting_for_the_kb(kb_dir):
    import threading
    import time

    from openkb.locks import LockCancelled, kb_ingest_lock
    from openkb.watch_service import WatchRegistry

    registry = WatchRegistry()
    results, errors = [], []

    def start():
        try:
            results.append(registry.start("pending", kb_dir, debounce=0.1))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=start)
    try:
        with kb_ingest_lock(kb_dir / ".openkb"):
            worker.start()
            deadline = time.monotonic() + 2
            stopped = registry.stop("pending")
            while not stopped and time.monotonic() < deadline:
                time.sleep(0.01)
                stopped = registry.stop("pending")
            assert stopped
        worker.join(5)
        assert not worker.is_alive()
        assert not results
        assert len(errors) == 1 and isinstance(errors[0], LockCancelled)
        assert not registry.status("pending")["active"]
    finally:
        worker.join(5)
        registry.stop_all()
