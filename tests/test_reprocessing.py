"""Explicit processing changes share the ordinary admission/publication contract."""

import pytest
from native_catalog_fixtures import catalog, original_bytes, retained_image_bytes

pytest_plugins = ("test_office_import", "block_fixtures", "workbook_fixtures")


def _published_input(kb_dir, path, *, summary_text=None):
    """Prepare manual domain state for policy checks from actual source text."""
    from publication_fixtures import publish_document

    from ultrakb.config import resolve_credential_bundle

    if summary_text is not None:
        body = summary_text
    elif path.suffix == ".pdf":
        import pymupdf

        with pymupdf.open(path) as document:
            body = "\n".join(page.get_text() for page in document)
    else:
        body = path.read_text("utf-8")
    return publish_document(kb_dir, path, body, bundle=resolve_credential_bundle(kb_dir))


def test_preview_domain_preparation_keeps_original_images_for_strict_compile_view(
    kb_dir, tmp_path, pdf_model, live_llm
):
    import hashlib

    from figure_fixtures import png_bytes
    from native_catalog_fixtures import read_source_revision

    from ultrakb.application.views import published_scope
    from ultrakb.documents import read_document_source
    from ultrakb.figure_assets import retained_figures
    from ultrakb.markdown_images import image_references
    from ultrakb.storage.compile_inputs import materialize_inputs

    original_text = "Stable input\n![image](image.png)"
    image = png_bytes()
    path = tmp_path / "assets.md"
    path.write_text(original_text, encoding="utf-8")
    (tmp_path / "image.png").write_bytes(image)
    requests = len(live_llm.calls)
    first = _published_input(kb_dir, path, summary_text="Stable input")
    assert first.status == "added", first.message
    source = read_document_source(kb_dir, first.source_id)
    original = read_source_revision(kb_dir, first.source_revision_id)
    assert original_bytes(kb_dir, original.revision_id).decode() == original_text
    [reference] = image_references(source["content"])
    assert reference.alt == "image" and reference.source == f"images/{source['doc_name']}/image.png"
    assert retained_image_bytes(kb_dir, source, f"sources/{reference.local_path}") == image
    [asset] = [file for file in original.files if file.digest == hashlib.sha256(image).hexdigest()]
    assert asset.digest == hashlib.sha256(image).hexdigest()
    assert catalog(kb_dir).read_file(original.revision_id, asset.path) == image
    base = published_scope(kb_dir).knowledge
    target = catalog(kb_dir).unit_revision(first.units[0].target_revision_id)
    with base.compile_workspace() as baseline:
        materialize_inputs(base, baseline, target)
        # This is the actual strict figure-preservation boundary used by a
        # reprocess. A relative source URL must never be copied into a summary.
        [figure] = list(retained_figures(baseline.directory).values())
        assert figure.asset_digest == asset.digest
        assert (baseline.directory / figure.asset_path).read_bytes() == image
        summary = baseline.directory / f"summaries/{first.units[0].doc_name}.md"
        assert "Stable input" in summary.read_text("utf-8")
        assert "![image](image.png)" not in summary.read_text("utf-8")
    assert len(live_llm.calls) == requests


def test_ordinary_retry_keeps_the_saved_processing_policy(kb_dir, tmp_path, pdf_model):
    from ultrakb.application.documents import import_document
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest

    path = tmp_path / "note.md"
    path.write_text("Stable input", encoding="utf-8")
    first = _published_input(kb_dir, path)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"model_capacity": {"max_input_tokens": 1100}}),
    )
    retried = import_document(kb_dir, path)
    assert retried.status == "skipped", retried.message
    assert retried.units[0].target_revision_id == first.units[0].target_revision_id


@pytest.mark.parametrize("kind", ["markdown", "pdf"])
def test_preview_is_read_only_and_execution_retains_the_original_and_history(
    kb_dir, tmp_path, physical_pdf, pdf_model, monkeypatch, kind
):
    from native_catalog_fixtures import read_source_revision

    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.documents import read_document_source

    path = physical_pdf
    if kind == "markdown":
        from figure_fixtures import png_bytes

        path = tmp_path / "assets.md"
        (tmp_path / "image.png").write_bytes(png_bytes())
        path.write_text("Stable input\n![image](image.png)", encoding="utf-8")
    # The raw image URL is relative to the source directory. This manual
    # summary needs only the source's text; native image bytes and references
    # remain in the untouched retained original and processing projection.
    first = _published_input(
        kb_dir, path, summary_text="Stable input" if kind == "markdown" else None
    )
    assert first.status == "added", first.message
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
        store = catalog(kb_dir)
        return (
            store.source(first.source_id),
            tuple(
                (unit, store.processing(unit.processing_id))
                for unit in store.source_units(first.source_id)
            ),
        )

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
    assert original_bytes(kb_dir, result.source_revision_id) == original_bytes(
        kb_dir, original.revision_id
    )
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
    from ultrakb.application.documents import import_document
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest

    path = tmp_path / "bad.pdf"
    path.write_bytes(physical_pdf.read_bytes())

    def conversion_failure(*args, **kwargs):
        raise ValueError("Fixture failed conversion after successful PDF text preflight")

    monkeypatch.setattr("ultrakb.converter.convert_pdf_with_images", conversion_failure)
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
    from office_publication_fixtures import publish_workbook
    from test_workbook_import import worksheet_prompt_markers

    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.config import resolve_credential_bundle
    from ultrakb.documents import read_document_source

    first = publish_workbook(kb_dir, three_sheets, bundle=resolve_credential_bundle(kb_dir))
    assert first.status == "added" and all(unit.status == "completed" for unit in first.units)
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["range"] == "all_worksheets" and len(preview["units"]) == 3
    complete = litellm.completion

    def reject_beta(**kwargs):
        if "BETA_SHEET" in worksheet_prompt_markers(kwargs["messages"]):
            raise ValueError("Local transport rejects Beta during reprocessing")
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


def test_cli_api_and_runtime_share_preview_and_stale_rejection(kb_dir, tmp_path, pdf_model):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from ultrakb.api import create_app
    from ultrakb.cli import cli
    from ultrakb.config import register_kb_alias
    from ultrakb.runtime.requests import ReprocessSource
    from ultrakb.runtime.tasks import TaskManager

    path = tmp_path / "adapter.md"
    path.write_text("One shared operation", encoding="utf-8")
    first = _published_input(kb_dir, path)
    assert first.status == "added", first.message
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
    kb_dir, tmp_path, pdf_model, monkeypatch
):
    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.storage.catalog_store import CatalogStore

    path = tmp_path / "repair.md"
    path.write_text("Original survives the missing processing cache", encoding="utf-8")
    first = _published_input(kb_dir, path)
    assert first.status == "added", first.message
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["status"] == "ready"
    second = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert second.status == "added", second.message
    read_original = CatalogStore.read_original

    def missing(self, revision_id):
        if revision_id == second.source_revision_id:
            raise FileNotFoundError("Retained original is unavailable")
        return read_original(self, revision_id)

    monkeypatch.setattr(CatalogStore, "read_original", missing)
    unavailable = preview_reprocessing(kb_dir, first.source_id)
    assert unavailable["status"] == "blocked" and not unavailable["original"]["available"]


def test_reprocessing_creates_an_independent_extraction_group(kb_dir, pdf_model):
    from pathlib import Path

    from office_publication_fixtures import publish_workbook

    from ultrakb.application.pending import pending_status, process_pending
    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.config import resolve_credential_bundle

    path = Path(__file__).parent / "fixtures/office/package-text.xls"
    first = publish_workbook(kb_dir, path, bundle=resolve_credential_bundle(kb_dir))
    assert first.status == "added", first.message
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


def test_reprocessing_preserves_model_quality_and_unfinished_stages():
    from ultrakb.agent.compile_plan import normalize_plan
    from ultrakb.compilation_report import (
        collect_compile_report,
        merge_compile_report,
        report_compile_issue,
    )
    from ultrakb.ingest_result import IngestResult

    # Untrusted parser input is exercised directly, without asking a model to
    # manufacture a malformed response or inventing a provider response.
    with collect_compile_report() as report:
        with pytest.raises(ValueError, match="create must be an array"):
            normalize_plan(
                {"create": "invalid-plan-shape", "update": [], "related": []},
                existing={"concepts": set(), "entities": set()},
                entity_types=frozenset({"other"}),
                sanitize=lambda name: name,
            )
        report_compile_issue("malformed_plan_items", "concepts", "entities")
        report_compile_issue("malformed_plan_items", "concepts")

    admitted = IngestResult("quality.md", "failed", (), quality=("retained_original",))
    result = merge_compile_report(admitted, report)
    assert result.quality == ("retained_original", "malformed_plan_items")
    assert set(result.unfinished) == {"concepts", "entities"}
    assert admitted.quality == ("retained_original",) and not admitted.unfinished


def test_execution_snapshot_must_match_the_reviewed_policy(kb_dir, physical_pdf, pdf_model):
    from native_catalog_fixtures import read_source

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.config_state import capture_config
    from ultrakb.locks import kb_ingest_lock

    first = _published_input(kb_dir, physical_pdf)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
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
    assert read_source(kb_dir, first.source_id).revision_id == first.source_revision_id


def test_retry_cannot_build_a_new_model_index_under_an_old_policy(
    kb_dir, physical_pdf, pdf_model, monkeypatch
):
    from ultrakb.application.documents import import_document
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.storage.knowledge_store import KnowledgeStore

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"model": "gpt-4o", "pdf_short_max_pages": 0}),
    )

    def fail_publication(*args, **kwargs):
        raise OSError("Fixture publication failure after index construction")

    with monkeypatch.context() as failure:
        failure.setattr(KnowledgeStore, "publish", fail_publication)
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

    from saved_processing_publication import publish_saved_processing

    from ultrakb.application.recompilation import recompile_document
    from ultrakb.application.reprocessing import preview_reprocessing
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.config import resolve_credential_bundle

    first = publish_saved_processing(
        kb_dir, writer_document, bundle=resolve_credential_bundle(kb_dir)
    )
    assert first.status == "added", first.message

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview and recompile must not launch Office")

    monkeypatch.setattr("ultrakb.office.runtime.probe", forbidden)
    assert preview_reprocessing(kb_dir, first.source_id)["status"] == "ready"
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"office_runtime_path": str(kb_dir / "missing-office")}
        ),
    )
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["status"] == "blocked" and not preview["runtime"]["available"]
    assert (
        asyncio.run(
            recompile_document(kb_dir, first.source_id, unit_id=first.units[0].unit_id)
        ).status
        == "compiled"
    )


def test_reprocessing_preserves_manual_edits_as_a_proposal(kb_dir, tmp_path, pdf_model):
    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.application.views import view_scope

    path = tmp_path / "manual-edit.md"
    path.write_text("Manual editing survives processing", encoding="utf-8")
    first = _published_input(kb_dir, path)
    assert first.status == "added", first.message
    scope = view_scope(kb_dir, first.units[0].view_id)
    summary = "summaries/manual-edit"
    page = read_page(kb_dir, summary, scope=scope)
    saved = save_page(kb_dir, summary, "Human knowledge", version=page.version, scope=scope)
    assert saved.status == "saved"
    preview = preview_reprocessing(kb_dir, first.source_id)
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert result.status == "blocked" and result.units[0].proposal_id
    assert "Human knowledge" in read_page(kb_dir, summary, scope=scope).body
    assert preview_reprocessing(kb_dir, first.source_id)["publication_impact"][
        "superseded_proposals"
    ] == [result.units[0].proposal_id]
