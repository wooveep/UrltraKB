"""Desktop workers retain useful progress even with discarded console streams."""

import asyncio
import io
import logging
import sys
from types import SimpleNamespace


def test_worker_captures_discarded_console_and_redacts_credentials(tmp_path, monkeypatch):
    from openkb.runtime import worker
    from openkb.runtime.records import UnitIdentity, UnitResult
    from openkb.runtime.requests import ImportFile

    messages = []

    class Channel:
        def __init__(self, *args):
            from threading import Event

            self.stopped = Event()
            self.budget_expired = Event()
            self.parent_gone = Event()
            self.truncated = False
            self.sequence = 0

        def event(self, data):
            self.sequence += 1
            messages.append(data)

        def snapshot(self, value):
            pass

        def send(self, *args, **kwargs):
            return True

    def execute(*args):
        print("Parsing PDF: chapter 1")
        sys.stderr.write("api_key=secret-")
        sys.stderr.flush()
        sys.stderr.write("fixture-value\n")
        logging.getLogger("pageindex.probe").warning("Retrying LLM completion (1/10)")
        logging.getLogger("LiteLLM").warning("LoggingWorker error: synthetic callback timeout")
        return UnitResult("completed")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-fixture-value")
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(worker, "WorkerChannel", Channel)
    monkeypatch.setattr(worker, "_execute", execute)
    request = ImportFile(str(tmp_path / "source.pdf"))
    identity = UnitIdentity.create("a" * 32, 0, str(tmp_path), request)
    pipe = SimpleNamespace(close=lambda: None)
    events = SimpleNamespace(cancel_join_thread=lambda: None, close=lambda: None)
    worker.run_unit(request, identity, None, tmp_path / "units", pipe, events)

    logs = list((tmp_path / "logs" / identity.task_id).glob("*.log"))
    assert logs, "Windowed worker discarded its diagnostic output"
    text = "".join(path.read_text(encoding="utf-8") for path in logs)
    assert "Parsing PDF: chapter 1" in text
    assert "Retrying LLM completion (1/10)" in text
    assert "secret-fixture-value" not in text
    assert "secret-" not in text
    assert any(m.get("event") == "diagnostic" for m in messages)
    from openkb.runtime.records import read_receipt

    receipt = read_receipt(tmp_path / "units", identity)
    assert receipt.status == "completed"
    assert receipt.warnings == ("model_logging_warning",)


def test_model_waits_are_visible_without_recording_prompts(tmp_path):
    from openkb.runtime.diagnostics import WorkerDiagnostics, install_llm_diagnostics

    events = []

    async def completion(**kwargs):
        await asyncio.sleep(0)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3))

    sdk = SimpleNamespace(completion=lambda **kwargs: None, acompletion=completion)
    with WorkerDiagnostics(tmp_path / "0.log", events.append) as diagnostics:
        install_llm_diagnostics(sdk)
        asyncio.run(sdk.acompletion(model="probe", messages=[{"content": "PRIVATE DOCUMENT"}]))
        diagnostics.pulse()
    assert sdk.acompletion is completion
    text = (tmp_path / "0.log").read_text(encoding="utf-8")
    assert "LLM #1" in text and "in=12" in text
    assert "PRIVATE DOCUMENT" not in text
    assert any(event.get("activity") is False for event in events)


def test_click_output_does_not_treat_diagnostics_as_binary(tmp_path):
    import click
    import pytest

    from openkb.runtime.diagnostics import WorkerDiagnostics

    with WorkerDiagnostics(tmp_path / "click.log", lambda _: None) as diagnostics:
        with pytest.raises(TypeError):
            diagnostics.stdout.write(b"")
        click.echo("Rollback failure diagnostic fixture")
    assert "Rollback failure diagnostic fixture" in (tmp_path / "click.log").read_text("utf-8")


def test_live_progress_is_saved_and_does_not_replace_answer_text(kb_dir, tmp_path):
    import json
    import queue
    from dataclasses import asdict

    from openkb.runtime.records import TaskView, UnitIdentity
    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager, _Task

    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        task_id = "b" * 32
        identity = UnitIdentity.create(
            task_id, 0, str(kb_dir), ImportFile(str(kb_dir / "source.pdf"))
        )
        view = TaskView(
            task_id, str(kb_dir), "ImportFile", "running", "preparing", 1, (), False, False
        )
        task = _Task(view, identities=(identity,))
        events = queue.Queue()
        attempt = SimpleNamespace(events=events, identity=identity, last_sequence=0)
        events.put({"identity": asdict(identity), "sequence": 1, "data": {"stage": "indexing"}})
        events.put(
            {
                "identity": asdict(identity),
                "sequence": 2,
                "data": {
                    "event": "diagnostic",
                    "text": "LLM #1 started",
                    "at": "2026-09-09T00:00:00+00:00",
                    "activity": True,
                },
            }
        )
        with manager._condition:
            manager._progress(task, attempt)
        saved = json.loads((manager.history_dir / f"{task_id}.json").read_text())
        assert saved["view"]["stage"] == "indexing"
        assert task.view.diagnostics == "LLM #1 started\n"
        assert task.view.text == ""
        assert task.view.last_activity_at == "2026-09-09T00:00:00+00:00"
        assert "diagnostics" not in saved["view"]
    finally:
        manager.shutdown(stop=True)
        assert manager.join(5)


def test_log_rotation_history_and_unavailable_disk(tmp_path):
    from openkb.log import DiagnosticLog
    from openkb.runtime.diagnostics import read_task_log

    path = tmp_path / "0.log"
    log = DiagnosticLog(path, max_bytes=120)
    log.include_secrets({"extra_headers": {"X-Auth": "fixture-header"}})
    log.write("first " + "x" * 150)
    log.write("last fixture-header messages=[PRIVATE DOCUMENT]")
    log.close()
    assert path.with_suffix(".log.1").exists()
    restored = read_task_log(tmp_path)
    assert "last" in restored and "first" not in restored
    assert "fixture-header" not in restored and "PRIVATE DOCUMENT" not in restored
    assert len(read_task_log(tmp_path, limit=20)) <= 20
    # A path under a regular file cannot be created; live diagnostics survive.
    unavailable = DiagnosticLog(path / "child.log")
    assert "still visible" in unavailable.write("still visible")[1]
    unavailable.close()


def test_task_details_refresh_while_open(tmp_path):
    import os
    import subprocess
    import textwrap

    import pytest

    pytest.importorskip("PySide6")
    script = textwrap.dedent("""\
        import sys
        from pathlib import Path
        from dataclasses import replace
        from types import SimpleNamespace
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTabWidget
        from openkb.desktop.task_details import show_task_details
        from openkb.runtime.records import TaskView

        app = QApplication([])
        view = TaskView("c" * 32, "/kb", "ImportFile", "running", "preparing",
                        1, (), False, False)
        manager = SimpleNamespace(history_dir=Path(sys.argv[1]), get=lambda _: view)
        observed = []
        def update():
            global view
            view = replace(view, stage="indexing", diagnostics="LLM #1 waiting for response")
        def inspect():
            dialog = QApplication.activeModalWidget()
            status = dialog.findChild(QPlainTextEdit, "task-status").toPlainText()
            logs = dialog.findChild(QPlainTextEdit, "task-log").toPlainText()
            observed.append("indexing" in status and "LLM #1" in logs)
            dialog.findChild(QTabWidget).setCurrentIndex(1)
            dialog.grab().save(str(Path(sys.argv[1]) / "task-log.png"))
            dialog.accept()
        QTimer.singleShot(50, update)
        QTimer.singleShot(1300, inspect)
        QTimer.singleShot(5000, app.quit)
        show_task_details(view, None, manager=manager)
        assert observed == [True], observed
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
