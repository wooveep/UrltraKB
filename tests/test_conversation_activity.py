"""Public activity contracts, reducer ordering and the independent desktop surface."""

import queue
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ultrakb.application.execution import ExecutionContext
from ultrakb.runtime.records import TaskView, UnitIdentity
from ultrakb.runtime.requests import AskQuestion
from ultrakb.runtime.tasks import TaskManager, _Task


def test_concurrent_activity_events_pair_and_keep_private_errors_out():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    events, ready = [], Barrier(2)
    context = ExecutionContext(on_event=events.append)

    def read(fail):
        try:
            with context.activity("reading"):
                ready.wait(timeout=5)
                if fail:
                    raise ValueError("private original and /private/path")
        except ValueError:
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(read, [False, True]))
    assert [e["data"]["status"] for e in events[:2]] == ["started", "started"]
    assert {e["data"]["status"] for e in events[2:]} == {"completed", "failed"}
    assert {e["data"]["activity_id"] for e in events[:2]} == {
        e["data"]["activity_id"] for e in events[2:]
    }
    assert "private" not in str(events)


def test_overlapping_activities_and_late_events_preserve_text_and_active_set(kb_dir, tmp_path):
    manager = TaskManager(history_dir=tmp_path / "activity-history")
    identity = UnitIdentity.create("a" * 32, 0, str(kb_dir), AskQuestion("Explain"))
    task = _Task(
        TaskView(
            "a" * 32,
            str(kb_dir),
            "AskQuestion",
            "running",
            "answering",
            1,
            (),
            False,
            False,
            text="Visible answer.",
        )
    )
    attempt = SimpleNamespace(identity=identity, events=queue.Queue(), last_sequence=0)
    sequence = 0

    def receive(data):
        nonlocal sequence
        sequence += 1
        attempt.events.put({"identity": asdict(identity), "sequence": sequence, "data": data})
        with manager._condition:
            manager._progress(task, attempt)

    context = ExecutionContext(on_event=receive, usage_task_id=identity.task_id)
    try:
        with context.activity("reading") as first:
            with context.activity("reading"):
                assert len(task.view.activities) == 2
                assert task.view.text == "Visible answer."
            assert len(task.view.activities) == 1
            assert task.view.activities[0]["activity_id"] == first["activity_id"]
            receive({"event": "tool_call", "data": {"name": "read_file"}})
            assert task.view.text == "Visible answer."
            attempt.events.put(
                {"identity": asdict(identity), "sequence": 1, "data": {"event": "answer_start"}}
            )
            with manager._condition:
                manager._progress(task, attempt)
            assert task.view.text == "Visible answer."
            wrong = asdict(identity) | {"task_id": "b" * 32}
            attempt.events.put(
                {"identity": wrong, "sequence": sequence + 1, "data": {"event": "answer_start"}}
            )
            with manager._condition:
                manager._progress(task, attempt)
            assert len(task.view.activities) == 1
        assert not task.view.activities
        assert task.view.completed_stages == ("reading",)
        with pytest.raises(ValueError):
            with context.activity("reading"):
                raise ValueError("private content")
        assert not task.view.activities
        receive({"event": "answer_start"})
        assert not task.view.text
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)


def test_activity_surface_keeps_stable_steps_counts_and_cleanup(kb_dir, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    from ultrakb.desktop.conversation_activity import ConversationActivity

    widget = ConversationActivity()
    task = TaskView(
        "a" * 32,
        str(kb_dir),
        "AskQuestion",
        "running",
        "reading",
        1,
        (),
        False,
        False,
        activities=({"activity_id": "x", "stage": "reading"},),
        completed_stages=("preparing", "retrieving"),
        source_count=2,
        unit_count=5,
    )
    widget.set_task(task)
    assert "正在阅读原文" in widget.text()
    for stage in ["准备上下文", "检索资料", "阅读原文", "组织答案", "保存"]:
        assert stage in widget.steps.text()
    assert "2 份资料" in widget.steps.text() and "5 个原生单元" in widget.steps.text()
    assert "%" not in widget.text() + widget.steps.text()
    widget.set_task(
        TaskView("a" * 32, str(kb_dir), "AskQuestion", "stopping", "reading", 1, (), True, False)
    )
    assert "等待资源清理" in widget.text()
    widget.setText("已停止")
    assert not widget.timer.isActive()
    widget.close()
    application.processEvents()
