"""Cross-entry contracts use real source state and only replace model I/O."""

import json

import pytest

pytest_plugins = ("test_workbook_import", "processing_fixtures")


def test_retry_source_uses_retained_input_after_original_was_removed(kb_dir, tmp_path):
    from publication_fixtures import publish_document

    from ultrakb.application.source_retry import retry_source
    from ultrakb.config import LlmCredentialBundle

    path = tmp_path / "note.md"
    path.write_text("Retained evidence", encoding="utf-8")
    first = publish_document(
        kb_dir, path, "Retained evidence", bundle=LlmCredentialBundle(api_key="local-domain")
    )
    path.unlink()
    from processing_fixtures import source_processing_context

    result = retry_source(kb_dir, first.source_id, context=source_processing_context())
    assert result.status == "skipped"
    assert result.source_revision_id == first.source_revision_id
    assert result.units[0].target_revision_id == first.units[0].target_revision_id


def test_cli_http_and_spawn_retry_share_source_revision_and_measurements(
    kb_dir, tmp_path, pdf_model
):
    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from ultrakb.api import create_app
    from ultrakb.application.documents import import_document
    from ultrakb.cli import cli
    from ultrakb.config import load_config, register_kb_alias, save_config
    from ultrakb.documents import read_document_source
    from ultrakb.runtime.assets import configure_sdk_resources
    from ultrakb.runtime.requests import RetrySource
    from ultrakb.runtime.tasks import TaskManager

    # Parent, CLI/HTTP, and the fresh child capture the same explicit model and
    # SDK table. A source retry cannot inherit a different user's role binding.
    configure_sdk_resources()
    config_path = kb_dir / ".ultrakb/config.yaml"
    config = load_config(config_path)
    config.update(model="deepseek/deepseek-flash", language="en")
    config["role_generation"] = {"compile": {"max_tokens": 1024, "reasoning_effort": "none"}}
    save_config(config_path, config)

    path = tmp_path / "cross.md"
    path.write_text("Inspect the same saved body.", encoding="utf-8")
    first = import_document(kb_dir, path)
    assert first.status == "added", first.message
    path.unlink()
    register_kb_alias("cross", kb_dir)
    expected = read_document_source(kb_dir, first.source_id)
    runner = CliRunner()
    shown = runner.invoke(cli, ["--kb-dir", str(kb_dir), "source", first.source_id])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["content"] == expected["content"]
    retried = runner.invoke(cli, ["--kb-dir", str(kb_dir), "retry-source", first.source_id])
    assert retried.exit_code == 0, retried.output
    cli_result = json.loads(retried.output)
    with TestClient(create_app()) as client:
        api = client.post(
            "/api/v1/document/retry", json={"kb": "cross", "source_id": first.source_id}
        )
        body = client.post("/api/v1/document/source", json={"kb": "cross", "hash": first.source_id})
    assert api.status_code == body.status_code == 200
    api_units = api.json()["units"]
    for api_unit, cli_unit in zip(api_units, cli_result["units"], strict=True):
        api_usage = api_unit.pop("model_usage")
        cli_usage = cli_unit.pop("model_usage")
        assert api_unit == cli_unit
        assert api_usage["current"]["requests"] == cli_usage["current"]["requests"] == 0
        assert api_usage["cumulative"] == cli_usage["cumulative"]
        assert api_usage["cumulative"]["requests"] > 0
        assert api_usage["execution_ids"] != cli_usage["execution_ids"]
    assert body.json()["content"] == expected["content"]
    assert api.json()["units"][0]["tokens"] == expected["tokens"]
    manager = TaskManager(history_dir=tmp_path / "tasks")
    try:
        task = manager.submit(kb_dir, [RetrySource(first.source_id)])
        result = manager.wait(task, timeout=30)
        assert result.state == "completed", result
        assert result.results[0].status == "skipped"
        assert result.model_usage["current"]["requests"] == 0
        assert result.model_usage["cumulative"] == first.model_usage["cumulative"]
        assert result.results[0].model_usage["cumulative"] == first.model_usage["cumulative"]
        assert first.units[0].target_revision_id in "\n".join(result.results[0].changes)
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_raw_watcher_excludes_managed_and_hidden_outputs(kb_dir):
    import threading

    from ultrakb.watcher import start_watch

    received = []
    completed = threading.Event()

    def observe(paths):
        received.extend(paths)
        if str(kb_dir / "raw/visible.md") in paths:
            completed.set()

    watch = start_watch(kb_dir / "raw", observe, debounce=0.05)
    try:
        managed = kb_dir / "raw/.ultrakb/normalized"
        managed.mkdir(parents=True)
        (managed / "derived.md").write_text("Never import", encoding="utf-8")
        (kb_dir / "wiki/sources/derived.md").write_text("Never import", encoding="utf-8")
        (kb_dir / "raw/visible.md").write_text("User input", encoding="utf-8")
        assert completed.wait(5)
    finally:
        watch.stop()
        watch.join(5)
    assert received == [str(kb_dir / "raw/visible.md")]


def test_failed_workbook_inventory_still_reports_its_discovery(kb_dir, pdf_model):
    from fastapi.testclient import TestClient

    from ultrakb.api import create_app
    from ultrakb.application.pending import pending_status
    from ultrakb.config import register_kb_alias

    register_kb_alias("counts", kb_dir)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "counts", "stream": "false"},
            files={"files": ("bad.xlsx", b"Invalid workbook body")},
        )
    assert response.status_code == 200, response.text
    result = response.json()
    current = pending_status(kb_dir)
    assert result["failed_count"] == 1
    assert result["discovery_pending_count"] == current["discovery_pending"] == 1


def test_add_api_keeps_compiler_quality(kb_dir, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from ultrakb.agent.compile_plan import normalize_plan
    from ultrakb.api import create_app
    from ultrakb.application.documents import import_document
    from ultrakb.compilation_report import CompilationIncomplete, report_compile_issue
    from ultrakb.config import register_kb_alias

    async def rejected_delivery(*args, **kwargs):
        # Inject a local validation failure at delivery; no provider response is
        # invented and no model is asked to manufacture invalid JSON.
        with pytest.raises(ValueError, match="create must be an array"):
            normalize_plan(
                {"create": "invalid-plan-shape", "update": [], "related": []},
                existing={"concepts": set(), "entities": set()},
                entity_types=frozenset({"other"}),
                sanitize=lambda name: name,
            )
        report_compile_issue("malformed_plan_items", "concepts", "entities")
        raise CompilationIncomplete(("malformed_plan_items",), ("concepts", "entities"))

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", rejected_delivery)
    source = tmp_path / "quality.md"
    source.write_text("Content with quality signals", encoding="utf-8")
    ordinary = import_document(kb_dir, source)
    assert ordinary.status == "failed"
    assert ordinary.quality and ordinary.unfinished, ordinary.message
    register_kb_alias("quality", kb_dir)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "quality", "stream": "false"},
            files={"files": ("quality-api.md", b"Content with quality signals")},
        )
    assert response.status_code == 200, response.text
    item = response.json()["files"][0]
    assert item["status"] == "failed"
    assert item.get("quality") == list(ordinary.quality)
    assert item.get("unfinished") == list(ordinary.unfinished)


def test_visible_markdown_reacts_to_hidden_asset_change(tmp_path):
    import threading

    from ultrakb.watcher import start_watch

    raw = tmp_path / "raw"
    images = raw / ".assets"
    images.mkdir(parents=True)
    image = images / "figure.png"
    image.write_bytes(b"original asset")
    doc = raw / "visible.md"
    doc.write_text("# Product V1 Manual\n\n![Diagram](.assets/figure.png)\n", encoding="utf-8")
    received = []
    changed = threading.Event()

    def callback(paths):
        received.extend(paths)
        if str(doc) in paths:
            changed.set()

    watch = start_watch(raw, callback, debounce=0.05)
    try:
        image.write_bytes(b"changed asset")
        assert changed.wait(2), received
    finally:
        watch.stop()
        watch.join(3)
    assert str(image) not in received
