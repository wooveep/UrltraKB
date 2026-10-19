"""Real spawn execution at the agreed task lifecycle/application boundary."""

from ultrakb.application.pages import read_page
from ultrakb.desktop.verification_data import publish_pages


def test_task_saves_page_in_spawn_worker_and_retains_confirmed_history(kb_dir, tmp_path):
    from ultrakb.runtime.requests import SavePage
    from ultrakb.runtime.tasks import TaskManager

    path = kb_dir / "wiki/concepts/attention.md"
    path.write_text("Original\n")
    publish_pages(kb_dir, {"concepts/attention.md": b"Original\n"})
    original = read_page(kb_dir, "concepts/attention")
    history = tmp_path / "task-history"
    manager = TaskManager(history_dir=history, max_workers=1)
    try:
        task_id = manager.submit(
            kb_dir, [SavePage("concepts/attention", "Changed\n", original.version)]
        )
        result = manager.wait(task_id, timeout=30)
        assert result.state == "completed"
        assert result.succeeded == 1
        assert result.processes_reaped
        assert read_page(kb_dir, "concepts/attention").body == "Changed\n"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)
    # History is a summary, not a replay queue or another copy of page text.
    assert "Changed" not in "".join(p.read_text() for p in history.rglob("*.json"))
    restarted = TaskManager(history_dir=history, max_workers=1)
    try:
        assert restarted.get(task_id).state == "completed"
        assert restarted.get(task_id).succeeded == 1
    finally:
        restarted.shutdown(stop=True)
        assert restarted.join(30)


def test_waiting_kb_does_not_starve_another_kb_and_stop_withdraws_waiter(kb_dir, tmp_path):
    import shutil
    import time

    from ultrakb.locks import kb_ingest_lock
    from ultrakb.runtime.requests import SavePage
    from ultrakb.runtime.tasks import TaskManager

    page = kb_dir / "wiki/concepts/attention.md"
    page.write_text("Original\n")
    publish_pages(kb_dir, {"concepts/attention.md": b"Original\n"})
    other = tmp_path.parent / (tmp_path.name + "-other")
    shutil.copytree(kb_dir, other)
    version = read_page(kb_dir, "concepts/attention").version
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    try:
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            waiting = manager.submit(kb_dir, [SavePage("concepts/attention", "Blocked", version)])
            independent = manager.submit(
                other, [SavePage("concepts/attention", "Independent", version)]
            )
            assert manager.wait(independent, timeout=30).state == "completed"
            deadline = time.monotonic() + 10
            while manager.get(waiting).state != "waiting" and time.monotonic() < deadline:
                time.sleep(0.02)
            assert manager.get(waiting).started_at is None
            manager.stop(waiting)
            result = manager.wait(waiting, timeout=30)
            assert result.state == "stopped"
            assert result.unfinished == 1
            assert page.read_text() == "Original\n"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)


def test_unsupported_storage_stops_the_batch_before_later_units(kb_dir, tmp_path):
    from ultrakb.runtime.requests import SavePage
    from ultrakb.runtime.tasks import TaskManager
    from ultrakb.storage.database import database_path

    publish_pages(kb_dir, {"concepts/attention.md": b"Original\n"})
    version = read_page(kb_dir, "concepts/attention").version
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    try:
        database_path(kb_dir).write_bytes(b"unsupported-master")
        task = manager.submit(
            kb_dir,
            [
                SavePage("concepts/attention", "First", version),
                SavePage("concepts/attention", "Must not start", version),
            ],
        )
        result = manager.wait(task, timeout=15)
        assert result.state == "blocked" and len(result.results) == 1, result
        assert result.results[0].halt and result.processes_reaped
        assert (kb_dir / "wiki/concepts/attention.md").read_text() == "Original\n"
        assert database_path(kb_dir).read_bytes() == b"unsupported-master"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_import_source_is_visible_during_the_actual_model_wait(kb_dir, tmp_path):
    import threading

    from error_transport import ErrorTransport

    from ultrakb.application.sources import source_inventory
    from ultrakb.runtime.requests import ImportFile
    from ultrakb.runtime.tasks import TaskManager

    source = tmp_path / "waiting-source.md"
    source.write_text("# Retained local input\n")
    gate = threading.Event()
    with ErrorTransport(status=400, gate=gate) as service:
        (kb_dir / ".env").write_text(f"LLM_API_KEY=offline-key\nOPENAI_API_BASE={service.url}\n")
        manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
        try:
            task_id = manager.submit(kb_dir, [ImportFile(str(source))])
            assert manager.get(task_id).current_source == str(source)
            assert service.wait_for_requests(1, timeout=15)
            assert manager.get(task_id).current_source == str(source)
            assert source_inventory(kb_dir)[0]["location"] == str(source)
            manager.stop(task_id)
            gate.set()
            assert manager.wait(task_id, timeout=15).state == "stopped"
        finally:
            gate.set()
            manager.shutdown(stop=True)
            assert manager.join(15)
