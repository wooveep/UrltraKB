"""Explicit processing changes share the ordinary admission/publication contract."""

import pytest

pytest_plugins = ("test_workbook_import",)


def test_ordinary_retry_keeps_the_saved_processing_policy(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    path = tmp_path / "note.md"
    path.write_text("Stable input", encoding="utf-8")
    first = import_document(kb_dir, path)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"model_capacity": {"max_input_tokens": 1}}),
    )
    retried = import_document(kb_dir, path)
    assert retried.status == "skipped", retried.message
    assert retried.units[0].target_revision_id == first.units[0].target_revision_id


@pytest.mark.parametrize("kind", ["markdown", "pdf"])
def test_preview_is_read_only_and_execution_retains_the_original_and_history(
    kb_dir, tmp_path, physical_pdf, pdf_model, monkeypatch, kind
):
    from openkb.application.documents import import_document
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source
    from openkb.source_catalog import read_source_revision
    from openkb.view_records import SourceMetadata

    path = physical_pdf
    if kind == "markdown":
        path = tmp_path / "assets.md"
        (tmp_path / "image.png").write_bytes(b"retained image")
        path.write_text("Stable input\n![image](image.png)", encoding="utf-8")
    first = import_document(
        kb_dir, path, metadata=SourceMetadata(product="Fixture", applicable_versions=("1",))
    )
    before = read_document_source(kb_dir, first.source_id)
    original = read_source_revision(kb_dir, first.source_revision_id)
    path.unlink()
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "pdf_short_max_pages": 12,
                "model_capacity": {"max_input_tokens": 100000, "output_reserve_tokens": 0},
            },
        ),
    )

    def snapshot():
        return {p: p.read_bytes() for p in (kb_dir / ".openkb/catalog").rglob("*.json")}

    saved = snapshot()
    with monkeypatch.context() as boundary:

        def forbidden(**kwargs):
            raise AssertionError("Preview must not call the model")

        boundary.setattr("litellm.completion", forbidden)
        boundary.setattr("litellm.acompletion", forbidden)
        preview = preview_reprocessing(kb_dir, first.source_id)
    assert snapshot() == saved
    assert preview["status"] == "ready" and preview["original"]["available"]
    assert preview["units"][0]["policy_changed"]
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert result.status == "added", result.message
    assert result.source_id == first.source_id
    assert result.units[0].target_revision_id != first.units[0].target_revision_id
    current = read_document_source(kb_dir, first.source_id)
    assert current["view_id"] == before["view_id"]
    assert current["content"] == before["content"]
    assert read_source_revision(kb_dir, result.source_revision_id).original == original.original
    history = read_document_source(
        kb_dir, first.source_id, source_revision_id=first.source_revision_id
    )
    assert history["content"] == before["content"]
    repeated = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert repeated.status == "skipped" and repeated.source_revision_id == result.source_revision_id
    with pytest.raises(ValueError, match="preview"):
        reprocess_source(kb_dir, first.source_id, version="0" * 64)


def test_failed_conversion_retry_requires_preview_if_policy_changed(
    kb_dir, tmp_path, pdf_model, physical_pdf, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    path = tmp_path / "bad.pdf"
    path.write_bytes(physical_pdf.read_bytes())

    def conversion_failure(*args, **kwargs):
        raise ValueError("Fixture failed conversion after successful PDF text preflight")

    monkeypatch.setattr("openkb.converter.convert_pdf_with_images", conversion_failure)
    first = import_document(kb_dir, path)
    assert first.status == "failed"
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 12})
    )
    again = import_document(kb_dir, path)
    assert again.status == "blocked" and "reprocess" in again.message.lower()
    assert again.units[0].target_revision_id == first.units[0].target_revision_id


def test_workbook_reprocessing_preserves_failed_sheet_and_shared_unit_identities(
    kb_dir, three_sheets, pdf_model, monkeypatch
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.documents import read_document_source

    first = import_document(kb_dir, three_sheets)
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["range"] == "all_worksheets" and len(preview["units"]) == 3
    complete = litellm.completion

    def reject_beta(**kwargs):
        if "BETA_SHEET" in str(kwargs["messages"]):
            raise ValueError("Fixture model fails Beta during reprocessing")
        return complete(**kwargs)

    async def areject_beta(**kwargs):
        return reject_beta(**kwargs)

    monkeypatch.setattr(litellm, "completion", reject_beta)
    monkeypatch.setattr(litellm, "acompletion", areject_beta)
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert result.status == "partial", result.message
    assert {u.name: u.unit_id for u in result.units} == {u.name: u.unit_id for u in first.units}
    beta = next(u for u in result.units if u.name == "Beta")
    retained = read_document_source(kb_dir, first.source_id, unit_id=beta.unit_id)
    assert retained["source_revision_id"] == first.source_revision_id
    assert retained["target_source_revision_id"] == result.source_revision_id
    assert "BETA_SHEET" in retained["content"]

    async def healthy(**kwargs):
        return complete(**kwargs)

    monkeypatch.setattr(litellm, "completion", complete)
    monkeypatch.setattr(litellm, "acompletion", healthy)
    resumed = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert resumed.status == "added" and resumed.source_revision_id == result.source_revision_id
    assert next(u.knowledge_revision_id for u in resumed.units if u.name == "Alpha") == next(
        u.knowledge_revision_id for u in result.units if u.name == "Alpha"
    )


def test_ordinary_workbook_retry_does_not_reparse_a_lost_inventory(kb_dir, three_sheets, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    first = import_document(kb_dir, three_sheets)
    inventory = kb_dir / ".openkb/catalog/workbooks" / f"{first.source_revision_id}.json"
    inventory.unlink()
    again = import_document(kb_dir, three_sheets)
    assert again.status == "blocked" and "reprocess" in again.message.lower()
    assert not inventory.exists()
    assert (
        "ALPHA_SHEET"
        in read_document_source(kb_dir, first.source_id, unit_id=first.units[0].unit_id)["content"]
    )


def test_cli_api_and_runtime_share_preview_and_stale_rejection(kb_dir, tmp_path, pdf_model):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.application.documents import import_document
    from openkb.cli import cli
    from openkb.config import register_kb_alias
    from openkb.runtime.requests import ReprocessSource
    from openkb.runtime.tasks import TaskManager

    path = tmp_path / "adapter.md"
    path.write_text("One shared operation", encoding="utf-8")
    first = import_document(kb_dir, path)
    command = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "reprocess", first.source_id])
    assert command.exit_code == 0, command.output
    preview = json.loads(command.output)
    register_kb_alias("processing", kb_dir)
    with TestClient(create_app()) as client:
        body = {"kb": "processing", "source_id": first.source_id}
        api_preview = client.post("/api/v1/document/reprocess/preview", json=body)
        assert api_preview.status_code == 200, api_preview.text
        assert api_preview.json()["version"] == preview["version"]
        result = client.post(
            "/api/v1/document/reprocess", json={**body, "version": preview["version"]}
        )
        assert result.status_code == 200 and result.json()["status"] == "added", result.text
        stale = client.post("/api/v1/document/reprocess", json={**body, "version": "0" * 64})
        assert stale.status_code == 409
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        task = manager.submit(kb_dir, [ReprocessSource(first.source_id, "0" * 64)])
        outcome = manager.wait(task)
        assert outcome.results[0].status == "blocked" and "preview" in str(outcome.results).lower()
    finally:
        manager.shutdown(stop=True)
        assert manager.join(30)


def test_missing_normalization_can_be_reprocessed_but_missing_original_cannot(
    kb_dir, tmp_path, pdf_model
):
    import shutil

    from openkb.application.documents import import_document
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.source_catalog import read_source_revision

    path = tmp_path / "repair.md"
    path.write_text("Original survives the missing processing cache", encoding="utf-8")
    first = import_document(kb_dir, path)
    shutil.rmtree(kb_dir / ".openkb/normalized")
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["status"] == "ready"
    second = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert second.status == "added", second.message
    (kb_dir / read_source_revision(kb_dir, second.source_revision_id).original).unlink()
    unavailable = preview_reprocessing(kb_dir, first.source_id)
    assert unavailable["status"] == "blocked" and not unavailable["original"]["available"]


def test_reprocessing_creates_an_independent_extraction_group(kb_dir, pdf_model):
    from pathlib import Path

    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source

    path = Path(__file__).parent / "fixtures/office/package-text.xls"
    first = import_document(kb_dir, path)
    process_pending(kb_dir, max_jobs=1)
    old = pending_status(kb_dir)
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert pending_status(kb_dir) == old and preview["discovery"]["will_schedule"]
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert result.status in {"added", "partial"}, result.message
    current = pending_status(kb_dir)
    assert len(current["groups"]) == len(old["groups"]) + 1
    old_jobs = {job["id"]: job for job in old["jobs"]}
    assert {job["id"]: job for job in current["jobs"] if job["id"] in old_jobs} == old_jobs


@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("same_bytes", [False, True])
def test_legacy_preview_requires_a_verified_original_and_never_maps_on_read(
    kb_dir, pdf_model, mapped, same_bytes
):
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.application.views import map_legacy_sources
    from openkb.documents import read_document_source
    from openkb.source_catalog import list_sources
    from openkb.state import HashRegistry

    raw = kb_dir / "raw/old.md"
    raw.write_text("Verified original", encoding="utf-8")
    old_body = kb_dir / "wiki/sources/old.md"
    legacy_content = "Verified original" if same_bytes else "Retained legacy normalization"
    old_body.write_text(legacy_content, encoding="utf-8")
    identity = HashRegistry.hash_file(raw)
    HashRegistry(kb_dir / ".openkb/hashes.json").add(
        identity,
        {
            "name": "old.md",
            "doc_name": "old",
            "type": "md",
            "raw_path": "raw/old.md",
            "source_path": "wiki/sources/old.md",
        },
    )
    if mapped:
        map_legacy_sources(kb_dir)
    before = list_sources(kb_dir)
    preview = preview_reprocessing(kb_dir, identity)
    assert preview["status"] == "ready" and list_sources(kb_dir) == before
    raw.unlink()
    unavailable = preview_reprocessing(kb_dir, identity)
    assert unavailable["status"] == "blocked" and not unavailable["original"]["available"]
    raw.write_text("Verified original", encoding="utf-8")
    preview = preview_reprocessing(kb_dir, identity)
    result = reprocess_source(kb_dir, identity, version=preview["version"])
    assert result.status == "blocked" and "version_metadata" in result.unfinished
    assert len(list_sources(kb_dir)) == 1
    assert old_body.read_text() == legacy_content
    assert read_document_source(kb_dir, result.source_id)["content"] == "Verified original"


def test_reprocessing_preserves_model_quality_and_unfinished_stages(
    kb_dir, tmp_path, pdf_model, monkeypatch
):
    import json
    from types import SimpleNamespace

    from openkb.application.documents import import_document
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source

    path = tmp_path / "quality.md"
    path.write_text("Quality signals", encoding="utf-8")
    first = import_document(kb_dir, path)

    def model(**kwargs):
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

    async def amodel(**kwargs):
        return model(**kwargs)

    monkeypatch.setattr("litellm.completion", model)
    monkeypatch.setattr("litellm.acompletion", amodel)
    preview = preview_reprocessing(kb_dir, first.source_id)
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert "malformed_plan_items" in result.quality
    assert set(result.unfinished) == {"concepts", "entities"}


def test_execution_snapshot_must_match_the_reviewed_policy(kb_dir, physical_pdf, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.config_state import capture_config
    from openkb.locks import kb_ingest_lock
    from openkb.source_catalog import read_source

    first = import_document(kb_dir, physical_pdf)
    with kb_ingest_lock(kb_dir / ".openkb"):
        captured = capture_config(kb_dir)
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 12})
    )
    preview = preview_reprocessing(kb_dir, first.source_id)
    with pytest.raises(ValueError, match="preview"):
        reprocess_source(
            kb_dir,
            first.source_id,
            version=preview["version"],
            context=ExecutionContext(snapshot=captured),
        )
    assert read_source(kb_dir, first.source_id).target_revision_id == first.source_revision_id


def test_retry_cannot_build_a_new_model_index_under_an_old_policy(
    kb_dir, physical_pdf, pdf_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.mutation import MutationSnapshot

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"model": "gpt-4o", "pdf_short_max_pages": 0}),
    )
    commit = MutationSnapshot.mark_committed

    def fail_publication(snapshot):
        if snapshot.operation == "publish-unit-revision":
            raise OSError("Fixture publication failure after index construction")
        commit(snapshot)

    with monkeypatch.context() as failure:
        failure.setattr(MutationSnapshot, "mark_committed", fail_publication)
        first = import_document(kb_dir, physical_pdf)
    assert first.status == "failed", first.message
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"model": "gpt-4o-mini"})
    )
    again = import_document(kb_dir, physical_pdf)
    assert again.status == "blocked" and "Index policy changed" in again.message
    assert again.units[0].target_revision_id == first.units[0].target_revision_id


def test_office_preview_checks_capability_without_launch_and_recompile_keeps_pdf(
    kb_dir, office_runtime, writer_document, pdf_model, monkeypatch
):
    import asyncio

    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document
    from openkb.application.reprocessing import preview_reprocessing
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    first = import_document(kb_dir, writer_document)
    assert first.status == "added", first.message

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview and recompile must not launch Office")

    monkeypatch.setattr("openkb.office.runtime.probe", forbidden)
    assert preview_reprocessing(kb_dir, first.source_id)["status"] == "ready"
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"office_runtime_path": str(kb_dir / "missing-office")}
        ),
    )
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["status"] == "blocked" and not preview["runtime"]["available"]
    assert asyncio.run(recompile_document(kb_dir, first.source_id)).status == "compiled"


def test_reprocessing_preserves_manual_edits_as_a_proposal(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.pages import read_page, save_page
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.application.views import view_scope

    path = tmp_path / "manual-edit.md"
    path.write_text("Manual editing survives processing", encoding="utf-8")
    first = import_document(kb_dir, path)
    scope = view_scope(kb_dir, first.units[0].view_id)
    summary = "summaries/manual-edit"
    page = read_page(kb_dir, summary, scope=scope)
    saved = save_page(kb_dir, summary, "Human knowledge", version=page.version, scope=scope)
    assert saved.status == "saved"
    preview = preview_reprocessing(kb_dir, first.source_id)
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert result.status == "blocked" and result.units[0].proposal_id
    assert "Human knowledge" in read_page(kb_dir, summary, scope=scope).body
    assert preview_reprocessing(kb_dir, first.source_id)["version_impact"][
        "superseded_proposals"
    ] == [result.units[0].proposal_id]


def test_admitted_reprocessing_request_keeps_policy_after_interrupted_planning(
    kb_dir, physical_pdf, pdf_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.mutation import MutationSnapshot
    from openkb.source_catalog import read_source

    first = import_document(kb_dir, physical_pdf)
    preview = preview_reprocessing(kb_dir, first.source_id)
    commit = MutationSnapshot.mark_committed

    def lost_receipt(snapshot):
        if snapshot.operation == "plan-import-unit":
            raise OSError("Fixture lost processing plan commit")
        commit(snapshot)

    with monkeypatch.context() as failure:
        failure.setattr(MutationSnapshot, "mark_committed", lost_receipt)
        with pytest.raises(OSError, match="lost processing"):
            reprocess_source(kb_dir, first.source_id, version=preview["version"])
    admitted = read_source(kb_dir, first.source_id).target_revision_id
    assert admitted != first.source_revision_id
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 0})
    )
    resumed = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert resumed.status == "blocked" and "policy changed" in resumed.message
    assert resumed.source_revision_id == admitted


def test_request_resume_rejects_foreign_source_revision(kb_dir, tmp_path, pdf_model):
    import json

    from openkb.application.documents import import_document
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.view_records import SourceMetadata

    a = tmp_path / "alpha.md"
    b = tmp_path / "beta.md"
    a.write_text("Alpha genuine evidence", encoding="utf-8")
    b.write_text("Beta unrelated evidence", encoding="utf-8")
    metadata = SourceMetadata(product="Fixture", applicable_versions=("1",))
    first = import_document(kb_dir, a, metadata=metadata)
    other = import_document(kb_dir, b, metadata=metadata)
    preview = preview_reprocessing(kb_dir, other.source_id)
    other_new = reprocess_source(kb_dir, other.source_id, version=preview["version"])
    assert other_new.status == "added", other_new
    record = kb_dir / ".openkb/catalog/sources" / f"{first.source_id}.json"
    data = json.loads(record.read_text())
    data["target_revision_id"] = other_new.source_revision_id
    record.write_text(json.dumps(data), encoding="utf-8")
    before = {p: p.read_bytes() for p in (kb_dir / ".openkb/catalog").rglob("*.json")}
    with pytest.raises(ValueError, match="same input"):
        preview_reprocessing(kb_dir, first.source_id)
    with pytest.raises(ValueError, match="same input"):
        reprocess_source(kb_dir, first.source_id, version=preview["version"])
    after = {p: p.read_bytes() for p in (kb_dir / ".openkb/catalog").rglob("*.json")}
    assert before == after


def test_legacy_preview_does_not_accept_a_normalized_body_as_original(kb_dir):
    from openkb.application.reprocessing import preview_reprocessing
    from openkb.state import HashRegistry

    normalized = kb_dir / "wiki/sources/old.md"
    normalized.write_text("Only the old normalized body remains", encoding="utf-8")
    identity = HashRegistry.hash_file(normalized)
    HashRegistry(kb_dir / ".openkb/hashes.json").add(
        identity,
        {
            "name": "old.md",
            "doc_name": "old",
            "type": "md",
            "raw_path": "raw/../wiki/sources/old.md",
            "source_path": "wiki/sources/old.md",
        },
    )
    result = preview_reprocessing(kb_dir, identity)
    assert result["status"] == "blocked", result
    assert not result["original"]["available"]
