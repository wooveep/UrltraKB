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

    def fail(*args, **kwargs):
        calls.append(1)
        raise RecoveryRequired("Repair required during compilation")

    monkeypatch.setattr("litellm.completion", fail)
    with pytest.raises(RecoveryRequired):
        import_document(kb_dir, source)
    assert len(calls) == 1


def test_rest_watch_stops_processing_on_required_repair(kb_dir, monkeypatch):
    import threading

    from openkb.mutation import RecoveryRequired
    from openkb.watch_service import WatchRegistry

    started = threading.Event()
    calls = []

    def fail_compile(**kwargs):
        calls.append(kwargs["messages"][-1]["content"])
        started.set()
        raise RecoveryRequired("Explicit repair required")

    monkeypatch.setattr("litellm.completion", fail_compile)
    registry = WatchRegistry()
    state = registry.start("test", kb_dir, debounce=0.1)
    try:
        (kb_dir / "raw/bad.md").write_text("# Bad\n")
        (kb_dir / "raw/later.md").write_text("# Later\n")
        assert started.wait(5)
        state.worker_thread.join(5)
        assert not registry.status("test")["active"]
        assert len(calls) == 1 and "# Bad" in calls[0]
        assert not (kb_dir / "wiki/sources/later.md").exists()
    finally:
        registry.stop_all()


@pytest.mark.parametrize("moment", ["prepare", "model"])
def test_rest_watch_keeps_raw_input_identity_when_a_file_becomes_a_symlink(
    kb_dir, monkeypatch, moment
):
    import json
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from openkb.state import HashRegistry
    from openkb.watch_service import WatchRegistry
    from tests.http_model_fixture import evidence_response

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
    compiled = []
    replaced = False
    original_open = Path.open

    def replace_input():
        nonlocal replaced
        if not replaced:
            source.unlink()
            source.symlink_to(outside)
            replaced = True

    def open_file(path, mode="r", *args, **kwargs):
        if path == source and mode == "rb" and moment == "prepare":
            replace_input()
        return original_open(path, mode, *args, **kwargs)

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        if payload["stage"] == "facts":
            compiled.extend(unit["text"] for unit in payload["units"])
            if moment == "model":
                replace_input()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(evidence_response(payload)))
                )
            ],
            usage=None,
        )

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr(Path, "open", open_file)
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
    assert all("Outside" not in text for text in compiled)
    if moment == "model":
        assert compiled == ["# Original"]
    entries = HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()
    assert all(item.get("path") != outside.as_posix() for item in entries.values())
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


@pytest.mark.parametrize("marker_failure", [False, True])
def test_failed_import_rollback_stays_blocked_after_the_io_error_is_gone(
    kb_dir, tmp_path, monkeypatch, marker_failure, model_service
):
    import openkb.knowledge_commit as knowledge
    import openkb.mutation as mutation
    from openkb.application.documents import import_document
    from openkb.mutation import RecoveryRequired, repair_marker

    source = tmp_path / "note.md"
    source.write_text("# Previously retained source\n")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed"
    old = kb_dir / "wiki/concepts/notes.md"
    previous = old.read_bytes()
    source.write_text("# New document\n")
    copy = mutation._copy_file_atomic
    write_json = mutation.atomic_write_json
    write_knowledge = knowledge.atomic_write_json

    def fail_commit(path, *args, **kwargs):
        if path == kb_dir / ".openkb/knowledge/baselines.json":
            raise OSError("Knowledge metadata write failed")
        return write_knowledge(path, *args, **kwargs)

    def fail_marker(path, *args, **kwargs):
        if path == repair_marker(kb_dir):
            raise OSError("Repair marker cannot be written")
        return write_json(path, *args, **kwargs)

    def fail_restore(src, dest, **kwargs):
        if dest == old:
            raise OSError("Cannot restore source")
        return copy(src, dest, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(knowledge, "atomic_write_json", fail_commit)
        patch.setattr(mutation, "_copy_file_atomic", fail_restore)
        if marker_failure:
            patch.setattr(mutation, "atomic_write_json", fail_marker)
        with pytest.raises(RecoveryRequired):
            import_document(kb_dir, source)
    assert repair_marker(kb_dir).is_file() is not marker_failure
    assert list((kb_dir / ".openkb/journal").glob("*.json"))
    with pytest.raises(RecoveryRequired):
        import_document(kb_dir, source)
    from openkb.application.repair import repair_knowledge_base

    assert repair_knowledge_base(kb_dir).repaired
    assert old.read_bytes() == previous
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
