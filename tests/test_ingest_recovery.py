"""Failed recovery halts import work until explicitly repaired."""

pytest_plugins = ("test_pdf_readback",)


def test_import_does_not_retry_compilation_after_recovery_is_required(
    kb_dir, tmp_path, monkeypatch
):
    from ultrakb.application.documents import import_document
    from ultrakb.mutation import RecoveryRequired

    source = tmp_path / "note.md"
    source.write_text("# New document\n")
    calls = []

    async def fail(*args, **kwargs):
        calls.append(1)
        raise RecoveryRequired("Repair required during compilation")

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", fail)
    result = import_document(kb_dir, source)
    assert result.status == "blocked" and "Repair required" in result.message
    assert len(calls) == 1


def test_rest_watch_stops_processing_on_required_repair(kb_dir, monkeypatch):
    import threading

    from ultrakb.mutation import RecoveryRequired
    from ultrakb.watch_service import WatchRegistry

    started = threading.Event()
    calls = []

    async def fail_compile(doc_name, *args, **kwargs):
        calls.append(doc_name)
        started.set()
        raise RecoveryRequired("Explicit repair required")

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", fail_compile)
    registry = WatchRegistry()
    state = registry.start("test", kb_dir, debounce=0.1)
    try:
        (kb_dir / "raw/bad.md").write_text("# Bad\n")
        (kb_dir / "raw/later.md").write_text("# Later\n")
        assert started.wait(5)
        state.worker_thread.join(5)
        assert not registry.status("test")["active"]
        assert len(calls) == 1 and calls[0].startswith("bad-")
        assert not (kb_dir / "wiki/sources/later.md").exists()
    finally:
        registry.stop_all()


def test_native_watch_does_not_follow_a_replaced_knowledge_base(kb_dir):
    import shutil
    import time

    from ultrakb.runtime.tasks import TaskManager
    from ultrakb.runtime.watch import NativeWatch

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

    from ultrakb.locks import LockCancelled, kb_ingest_lock
    from ultrakb.watch_service import WatchRegistry

    registry = WatchRegistry()
    results, errors = [], []

    def start():
        try:
            results.append(registry.start("pending", kb_dir, debounce=0.1))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=start)
    try:
        with kb_ingest_lock(kb_dir / ".ultrakb"):
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
