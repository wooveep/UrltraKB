"""Cross-entry contracts use real source state and only replace model I/O."""

import json

import pytest

pytest_plugins = ("test_workbook_import",)


def test_retry_source_uses_retained_input_after_original_was_removed(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.source_retry import retry_source

    path = tmp_path / "note.md"
    path.write_text("Retained evidence", encoding="utf-8")
    first = import_document(kb_dir, path)
    path.unlink()
    result = retry_source(kb_dir, first.source_id)
    assert result.status == "skipped"
    assert result.source_revision_id == first.source_revision_id
    assert result.units[0].target_revision_id == first.units[0].target_revision_id


def test_cli_http_and_spawn_retry_share_source_revision_and_measurements(
    kb_dir, tmp_path, pdf_model
):
    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.application.documents import import_document
    from openkb.cli import cli
    from openkb.config import register_kb_alias
    from openkb.documents import read_document_source
    from openkb.runtime.requests import RetrySource
    from openkb.runtime.tasks import TaskManager

    path = tmp_path / "cross.md"
    path.write_text("Inspect the same saved body.", encoding="utf-8")
    first = import_document(kb_dir, path)
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
        assert first.units[0].target_revision_id in "\n".join(result.results[0].changes)
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_legacy_read_is_unknown_and_does_not_change_catalog(kb_dir, monkeypatch):
    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.cli import cli
    from openkb.config import register_kb_alias
    from openkb.state import HashRegistry

    (kb_dir / "wiki/sources/old.md").write_text("Preserved legacy body", encoding="utf-8")
    HashRegistry(kb_dir / ".openkb/hashes.json").add(
        "f" * 64, {"name": "old.docx", "doc_name": "old", "type": "docx"}
    )
    register_kb_alias("old", kb_dir)

    def forbidden(**kwargs):
        raise AssertionError("Reading legacy data must not call a model")

    monkeypatch.setattr("litellm.completion", forbidden)
    monkeypatch.setattr("litellm.acompletion", forbidden)
    before = {p: p.read_bytes() for p in (kb_dir / ".openkb/catalog").rglob("*.json")}
    result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "source", "f" * 64])
    assert result.exit_code == 0, result.output
    with TestClient(create_app()) as client:
        result = client.post("/api/v1/document/source", json={"kb": "old", "hash": "f" * 64})
    assert result.status_code == 200, result.text
    assert result.json()["content"] == "Preserved legacy body"
    assert result.json()["length_class"] is None and result.json()["tokens"] is None
    assert before == {p: p.read_bytes() for p in (kb_dir / ".openkb/catalog").rglob("*.json")}


def test_raw_watcher_excludes_managed_and_hidden_outputs(kb_dir):
    import threading

    from openkb.watcher import start_watch

    received = []
    completed = threading.Event()

    def observe(paths):
        received.extend(paths)
        if str(kb_dir / "raw/visible.md") in paths:
            completed.set()

    watch = start_watch(kb_dir / "raw", observe, debounce=0.05)
    try:
        managed = kb_dir / "raw/.openkb/normalized"
        managed.mkdir(parents=True)
        (managed / "derived.md").write_text("Never import", encoding="utf-8")
        (kb_dir / "wiki/sources/derived.md").write_text("Never import", encoding="utf-8")
        (kb_dir / "raw/visible.md").write_text("User input", encoding="utf-8")
        assert completed.wait(5)
    finally:
        watch.stop()
        watch.join(5)
    assert received == [str(kb_dir / "raw/visible.md")]


@pytest.mark.parametrize(
    "change", [{"minimum_writer_version": 999}, {"required_capabilities": ["future-inputs"]}]
)
def test_future_catalog_blocks_settings_and_cancel_signals_but_allows_read(
    kb_dir, tmp_path, pdf_model, change
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source
    from openkb.lifecycle import current_generation
    from openkb.pending.control import request_stop

    path = tmp_path / "note.md"
    path.write_text("Read remains available", encoding="utf-8")
    source = import_document(kb_dir, path)
    schema = kb_dir / ".openkb/catalog/schema.json"
    value = json.loads(schema.read_text())
    schema.write_text(json.dumps({**value, **change}))
    before = (kb_dir / ".openkb/config.yaml").read_bytes()
    with pytest.raises(ValueError):
        apply_kb_config_patch(
            kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"language": "en"})
        )
    with pytest.raises(ValueError):
        request_stop(kb_dir, "a" * 32, "b" * 32, current_generation(kb_dir))
    assert not (kb_dir / ".openkb/pending-control").exists()
    assert (kb_dir / ".openkb/config.yaml").read_bytes() == before
    assert "Read remains available" in read_document_source(kb_dir, source.source_id)["content"]


def unsupported_catalog(kb_dir):
    from openkb.catalog_schema import CatalogSchema, catalog_schema_path

    path = catalog_schema_path(kb_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = CatalogSchema().model_dump(mode="json")
    record["required_capabilities"].append("future-writer-v999")
    path.write_text(json.dumps(record), encoding="utf-8")


def test_cancel_does_not_write_to_an_unsupported_catalog(kb_dir, tmp_path):
    from openkb.application.pending import cancel_execution_group
    from openkb.inputs import prepared_input
    from openkb.source_catalog import admit_source_revision

    source = tmp_path / "source.md"
    source.write_text("Simple retained original", encoding="utf-8")
    with prepared_input(source) as prepared:
        admission = admit_source_revision(kb_dir, prepared)
    identity = admission.discovery_intent.root_import_id
    unsupported_catalog(kb_dir)
    with pytest.raises(ValueError):
        cancel_execution_group(kb_dir, identity)
    assert not (kb_dir / ".openkb/pending-control" / f"group-{identity}.json").exists()


def test_runtime_can_still_stop_when_catalog_becomes_unsupported(kb_dir, tmp_path):
    from openkb.application.pending import claim_pending_job
    from openkb.inputs import prepared_input
    from openkb.locks import kb_ingest_lock
    from openkb.runtime.requests import RunPendingJob
    from openkb.runtime.tasks import TaskManager
    from openkb.source_catalog import admit_source_revision

    path = tmp_path / "unconverted.docx"
    path.write_bytes(b"Retain admission before body processing")
    with prepared_input(path) as prepared:
        admit_source_revision(kb_dir, prepared)
    job = claim_pending_job(kb_dir)
    manager = TaskManager(history_dir=tmp_path / "tasks")
    try:
        with kb_ingest_lock(kb_dir / ".openkb"):
            task = manager.submit(kb_dir, [RunPendingJob(job["id"], job["dispatch_id"])])
            unsupported_catalog(kb_dir)
            manager.stop(task)
        assert manager.wait(task, timeout=20).state in {"stopped", "blocked"}
        assert not (kb_dir / ".openkb/pending-control").exists()
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_repair_does_not_roll_back_an_unsupported_catalog(kb_dir):
    from openkb.application.repair import repair_knowledge_base
    from openkb.mutation import snapshot_paths

    page = kb_dir / "wiki/concepts/future.md"
    page.write_text("Before mutation\n", encoding="utf-8")
    snapshot = snapshot_paths(kb_dir, [page], operation="future-fixture")
    page.write_text("Pending future mutation\n", encoding="utf-8")
    unsupported_catalog(kb_dir)
    try:
        repair_knowledge_base(kb_dir)
    except ValueError:
        pass
    assert page.read_text() == "Pending future mutation\n"
    assert snapshot.journal_path.exists()


def test_resuming_deletion_does_not_bypass_catalog_guard(kb_dir, monkeypatch):
    from openkb import kb_admin

    def fail_removal(path):
        raise OSError("Fixture interrupted deletion before directory removal")

    with monkeypatch.context() as failure:
        failure.setattr(kb_admin.shutil, "rmtree", fail_removal)
        with pytest.raises(OSError, match="interrupted deletion"):
            kb_admin.delete_kb(kb_dir)
    unsupported_catalog(kb_dir)
    try:
        kb_admin.delete_kb(kb_dir)
    except ValueError:
        pass
    assert kb_dir.exists(), "Unsupported KB was removed while resuming deletion"


def test_failed_workbook_inventory_still_reports_its_discovery(kb_dir, pdf_model):
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.application.pending import pending_status
    from openkb.config import register_kb_alias

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


def test_add_api_keeps_compiler_quality(kb_dir, tmp_path, pdf_model, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.application.documents import import_document
    from openkb.config import register_kb_alias

    def completion(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "description": "Fixture",
                                "content": "Body",
                                "create": "invalid-plan-shape",
                                "update": [],
                                "related": [],
                            }
                        )
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    source = tmp_path / "quality.md"
    source.write_text("Content with quality signals", encoding="utf-8")
    ordinary = import_document(kb_dir, source)
    assert ordinary.status == "added"
    assert ordinary.quality and ordinary.unfinished
    register_kb_alias("quality", kb_dir)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "quality", "stream": "false"},
            files={"files": ("quality-api.md", b"Content with quality signals")},
        )
    assert response.status_code == 200, response.text
    item = response.json()["files"][0]
    assert item.get("quality") == list(ordinary.quality)
    assert item.get("unfinished") == list(ordinary.unfinished)


def test_visible_markdown_reacts_to_hidden_asset_change(tmp_path):
    import threading

    from openkb.watcher import start_watch

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


def test_schema_restored_by_recovery_is_revalidated_before_writer_upgrade(kb_dir):
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.catalog_schema import CatalogSchema, UnsupportedCatalogWriter, catalog_schema_path
    from openkb.mutation import snapshot_paths

    schema = catalog_schema_path(kb_dir)
    schema.parent.mkdir(parents=True, exist_ok=True)
    old = CatalogSchema().model_dump(mode="json")
    old["required_capabilities"].append("future-writer-v999")
    schema.write_text(json.dumps(old), encoding="utf-8")
    snapshot_paths(kb_dir, [schema], operation="interrupted-schema-update")
    schema.write_text(CatalogSchema().model_dump_json(), encoding="utf-8")
    before = (kb_dir / ".openkb/config.yaml").read_bytes()
    try:
        apply_kb_config_patch(
            kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"language": "fr"})
        )
    except UnsupportedCatalogWriter:
        pass
    assert (kb_dir / ".openkb/config.yaml").read_bytes() == before
    assert "future-writer-v999" in json.loads(schema.read_text())["required_capabilities"]
