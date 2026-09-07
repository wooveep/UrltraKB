"""Real spawn execution at the agreed task lifecycle/application boundary."""

from openkb.application.pages import read_page


def test_task_saves_page_in_spawn_worker_and_retains_confirmed_history(kb_dir, tmp_path):
    from openkb.runtime.requests import SavePage
    from openkb.runtime.tasks import TaskManager

    path = kb_dir / "wiki/concepts/attention.md"
    path.write_text("Original\n")
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

    from openkb.locks import kb_ingest_lock
    from openkb.runtime.requests import SavePage
    from openkb.runtime.tasks import TaskManager

    page = kb_dir / "wiki/concepts/attention.md"
    page.write_text("Original\n")
    other = tmp_path.parent / (tmp_path.name + "-other")
    shutil.copytree(kb_dir, other)
    version = read_page(kb_dir, "concepts/attention").version
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    try:
        with kb_ingest_lock(kb_dir / ".openkb"):
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


def test_repair_block_stops_batch_before_starting_more_units(kb_dir, tmp_path):
    from openkb.runtime.requests import SavePage
    from openkb.runtime.tasks import TaskManager

    page = kb_dir / "wiki/concepts/attention.md"
    page.write_text("Original\n")
    version = read_page(kb_dir, "concepts/attention").version
    journal = kb_dir / ".openkb/journal/broken.json"
    journal.parent.mkdir()
    journal.write_text("{incomplete")
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    try:
        task = manager.submit(
            kb_dir,
            [
                SavePage("concepts/attention", "First", version),
                SavePage("concepts/attention", "Must not start", version),
            ],
        )
        result = manager.wait(task, timeout=30)
        assert result.state == "blocked"
        assert len(result.results) == 1
        assert result.unfinished == 2
        assert page.read_text() == "Original\n"
        assert journal.read_text() == "{incomplete"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)
