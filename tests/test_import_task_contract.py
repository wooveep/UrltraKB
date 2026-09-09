"""CLI/REST/task contracts over the real document operation and an HTTP model."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openkb.api import create_app
from openkb.runtime.requests import ImportFile
from openkb.runtime.tasks import TaskManager


def test_task_keeps_unfinished_document_separate_from_completed_items(
    kb_dir, tmp_path, model_service
):
    source = kb_dir / "notes.md"
    source.write_text("Original knowledge.")
    invalid = kb_dir / "bad.bin"
    invalid.write_bytes(b"unsupported")
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        task_id = manager.submit(kb_dir, [ImportFile(str(invalid)), ImportFile(str(source))])
        result = manager.wait(task_id, timeout=20)
        assert result.state == "partial"
        assert result.succeeded == 1 and result.failed == 1
        assert result.results[-1].document.knowledge_compilation == "completed"
        assert result.processes_reaped
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


@pytest.mark.parametrize("stream", ["true", "false"])
def test_rest_returns_reconnectable_task_and_the_same_document_contract(
    kb_dir, tmp_path, monkeypatch, model_service, stream
):
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "test-kb", "stream": stream, "task_id": "a" * 32},
            files=[("files", ("notes.md", b"Original knowledge.", "text/markdown"))],
        )
        assert response.status_code == 200, response.text
        if stream == "true":
            blocks = [block.splitlines() for block in response.text.strip().split("\n\n")]
            assert json.loads(blocks[0][1][6:])["task_id"] == "a" * 32
            payload = next(
                json.loads(lines[1][6:]) for lines in blocks if lines[0] == "event: result"
            )
        else:
            payload = response.json()
        assert payload["task_id"] == "a" * 32
        assert payload["files"][0]["document"]["knowledge_compilation"] == "completed"
        assert client.get("/api/v1/tasks/" + "a" * 32).json()["state"] == "completed"
        before = len(model_service)
        repeated = client.post(
            "/api/v1/add",
            data={"kb": "test-kb", "stream": "false", "task_id": "a" * 32},
            files=[("files", ("notes.md", b"Original knowledge.", "text/markdown"))],
        )
        assert repeated.status_code == 200
        assert len(model_service) == before
        conflict = client.post(
            "/api/v1/add",
            data={"kb": "test-kb", "stream": "false", "task_id": "a" * 32},
            files=[("files", ("notes.md", b"Different input.", "text/markdown"))],
        )
        assert conflict.status_code == 409
    assert not any(Path(row["document"]["source"]).exists() for row in payload["results"])


def test_rest_observation_disconnect_retains_input_until_explicit_stop(
    kb_dir, tmp_path, model_service
):
    import asyncio
    import io

    from fastapi import UploadFile

    from openkb.api_tasks import ImportTasks
    from openkb.api_uploads import reserve_upload

    upload, _ = reserve_upload(UploadFile(filename="notes.md", file=io.BytesIO()))
    upload.write_text("Owned until execution stops")
    model_service.release.clear()

    async def scenario():
        service = ImportTasks(tmp_path / "history")
        try:
            task_id = await service.accept(kb_dir, [(upload, upload.name)], None)
            events = service.events("test", task_id)
            assert "event: start" in await anext(events)
            await events.aclose()  # Disconnect only this observer.
            assert await asyncio.to_thread(model_service.received.wait, 15)
            assert upload.exists()
            assert service.manager.get(task_id).state not in {"stopped", "completed"}
            service.manager.stop(task_id)
            view = await asyncio.to_thread(service.manager.wait, task_id, timeout=15)
            assert view.state == "stopped"
            assert view.processes_reaped
            before = list((kb_dir / "wiki/summaries").iterdir())
            model_service.release.set()  # Late HTTP response cannot write knowledge.
            await asyncio.sleep(0.2)
            assert list((kb_dir / "wiki/summaries").iterdir()) == before == []
            assert len(model_service) == 1
        finally:
            model_service.release.set()
            await service.close()
        assert not upload.exists()

    asyncio.run(scenario())


def test_document_deadline_is_unfinished_and_does_not_stop_the_next_item(
    kb_dir, tmp_path, model_service
):
    import yaml

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(document_timeout=1.5, request_timeout=1, stage_timeout=1.5)
    config_path.write_text(yaml.safe_dump(config))
    source = kb_dir / "slow.md"
    source.write_text("Synthetic slow model input")
    invalid = kb_dir / "bad.bin"
    invalid.write_bytes(b"invalid")
    model_service.release.clear()
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        task_id = manager.submit(kb_dir, [ImportFile(str(source)), ImportFile(str(invalid))])
        view = manager.wait(task_id, timeout=20)
        assert view.state == "partial"
        assert view.results[0].status == "unfinished"
        assert view.results[1].status == "failed"
        assert not view.stop_requested
        assert view.processes_reaped
        assert not (kb_dir / "wiki/summaries/slow.md").exists()
    finally:
        model_service.release.set()
        manager.shutdown(stop=True)
        assert manager.join(10)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX SIGINT driver")
def test_cli_interrupt_returns_130_after_worker_recovery(kb_dir, tmp_path, model_service):
    import os
    import signal
    import subprocess
    import sys

    source = kb_dir / "stopped.md"
    source.write_text("Synthetic stopped document")
    model_service.release.clear()
    script = (
        "from pathlib import Path; import openkb.config as config; "
        f"config.GLOBAL_CONFIG_DIR=Path({str(tmp_path / 'config')!r}); "
        "from openkb.cli import cli; cli()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, "add", str(source)],
        cwd=kb_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
    )
    try:
        assert model_service.received.wait(15)
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=20)
        assert process.returncode == 130, output
        assert not (kb_dir / "wiki/summaries/stopped.md").exists()
        records = list((tmp_path / "config/cli/tasks").glob("*.json"))
        assert len(records) == 1
        view = json.loads(records[0].read_text())["view"]
        assert view["state"] == "stopped" and view["processes_reaped"]
        assert len(model_service) == 1
    finally:
        model_service.release.set()
        if process.poll() is None:
            process.kill()
            process.wait(10)


def _hang_after_unit(*args):
    import time

    from openkb.runtime.worker import run_unit

    run_unit(*args)
    time.sleep(30)


def test_recovered_teardown_preserves_receipt_and_continues_batch(
    kb_dir, tmp_path, monkeypatch, model_service
):
    import yaml

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"]["cleanup_timeout"] = 2
    config_path.write_text(yaml.safe_dump(config))
    first = kb_dir / "first.md"
    second = kb_dir / "second.md"
    first.write_text("First source")
    second.write_text("Second source")
    monkeypatch.setattr("openkb.runtime.tasks.run_unit", _hang_after_unit)
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        task_id = manager.submit(kb_dir, [ImportFile(str(first)), ImportFile(str(second))])
        view = manager.wait(task_id, timeout=25)
        assert view.state == "completed"
        assert view.succeeded == 2 and view.processes_reaped
        assert all("worker_termination_recovered" in row.warnings for row in view.results)
        assert len(model_service) == 6
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_reconnect_returns_interrupted_history_without_replaying(kb_dir, tmp_path):
    import asyncio

    from openkb.api_tasks import ImportTasks
    from openkb.runtime.records import TaskView

    history = tmp_path / "history"
    history.mkdir()
    task_id = "b" * 32
    previous = TaskView(
        task_id, str(kb_dir), "ImportFile", "running", "compiling", 1, (), False, False
    )
    (history / f"{task_id}.json").write_text(
        json.dumps({"view": previous.summary(), "identities": []})
    )

    async def scenario():
        service = ImportTasks(history)
        try:
            payload = await asyncio.wait_for(service.result("test", task_id), timeout=1)
            assert payload["state"] == "interrupted"
            assert not payload["processes_reaped"] and not payload["stop_confirmed"]
            with pytest.raises(RuntimeError, match="already owns"):
                ImportTasks(history)
        finally:
            await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "document",
    [
        {"source": 7, "status": "added", "resources": []},
        {"source": "note", "status": "added", "resources": "x"},
        {"source": "note", "status": "added", "resources": [], "source_intake": "imaginary"},
        {"source": "note", "status": "added", "resources": [], "usage": {"requests": "wrong"}},
    ],
)
def test_corrupt_document_history_is_rejected(document):
    from openkb.runtime.records import UnitResult

    with pytest.raises(ValueError):
        UnitResult.from_summary({"status": "completed", "document": document})


def test_slow_drip_http_response_obeys_elapsed_request_deadline(kb_dir, model_service):
    import time

    import yaml

    from openkb.application.documents import import_document

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"]["request_timeout"] = 0.1
    config_path.write_text(yaml.safe_dump(config))
    model_service.drip_seconds = 0.04
    source = kb_dir / "slow-drip.md"
    source.write_text("Synthetic slow-drip test")
    started = time.monotonic()
    result = import_document(kb_dir, source)
    assert result.status == "unfinished"
    assert result.reason == "request_timeout"
    assert time.monotonic() - started < 1
    assert result.usage["observable_attempts"] == 1
    assert result.usage["unknown_usage"] == 1
    assert not (kb_dir / "wiki/summaries/slow-drip.md").exists()


def test_corrupt_pdf_reports_conversion_failure(kb_dir):
    from openkb.application.documents import import_document

    source = kb_dir / "corrupt.pdf"
    source.write_bytes(b"not a PDF")
    result = import_document(kb_dir, source)
    assert result.status == "failed"
    assert result.stage == "parsing"
    assert result.reason.startswith("parsing_failed:")
    assert result.source_intake == "saved"


def test_cli_recompile_retains_manual_page_and_reports_reviewable_unfinished_result(
    kb_dir, tmp_path, monkeypatch, model_service
):
    from click.testing import CliRunner

    from openkb.application.documents import import_document
    from openkb.application.source_history import source_status
    from openkb.cli import cli

    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
    source = kb_dir / "notes.md"
    source.write_text("Saved source for recompilation")
    original = import_document(kb_dir, source)
    index = kb_dir / "wiki/index.md"
    index.write_text("# Human-maintained index\nKeep this explanation.\n")
    source.unlink()
    result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "recompile", "notes.md"])
    assert result.exit_code == 1, result.output
    assert "intake=saved, compilation=unfinished" in result.output
    assert "needs_acceptance" in result.output
    assert index.read_text() == "# Human-maintained index\nKeep this explanation.\n"
    status = source_status(kb_dir, original.source_id)
    assert status["result"]["reason"] == "needs_acceptance"
    records = list((tmp_path / "config/cli/tasks").glob("*.json"))
    assert len(records) == 1
    view = json.loads(records[0].read_text())["view"]
    assert view["state"] == "partial" and view["processes_reaped"]
