"""A failed same-original reprocess must not erase the native reading entry."""

import pytest

from openkb.agent.query_evidence import selection_catalog
from openkb.application.documents import import_document
from openkb.application.query_views import read_query_page, resolve_query_views
from openkb.application.reprocessing import preview_reprocessing, reprocess_source
from openkb.source_reading import source_capabilities
from openkb.unit_publication import read_head

pytest_plugins = ("test_workbook_import",)


def write_original(path, text):
    if path.suffix == ".md":
        path.write_text(text)
    else:
        import pymupdf

        path.unlink(missing_ok=True)
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), text)
            pdf.save(path)


@pytest.fixture(params=["markdown", "pdf"])
def failed_reprocess(kb_dir, tmp_path, monkeypatch, pdf_model, request):
    import litellm

    path = tmp_path / ("operations.md" if request.param == "markdown" else "operations.pdf")
    write_original(path, "The pump must stop before the inspection valve opens.")
    first = import_document(kb_dir, path)
    assert first.status == "added", first.message
    head = read_head(kb_dir, "legacy")
    before = resolve_query_views(kb_dir)
    assert "summaries/operations.md" in before.views[0].files
    source = next(iter(source_capabilities(before.views[0]).values()))
    original = (before.views[0].scope.wiki_dir / source["path"]).read_bytes()
    completion, acompletion = litellm.completion, litellm.acompletion

    def rejected(**kwargs):
        raise ValueError("Offline fixture: new summary cannot be generated")

    async def arejected(**kwargs):
        return rejected(**kwargs)

    monkeypatch.setattr("litellm.completion", rejected)
    monkeypatch.setattr("litellm.acompletion", arejected)
    preview = preview_reprocessing(kb_dir, first.source_id)
    failed = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert failed.status == "failed", failed.message
    assert read_head(kb_dir, "legacy").knowledge_revision_id == head.knowledge_revision_id
    return first, failed, original, path, preview, completion, acompletion


def test_failed_same_original_reprocess_keeps_labelled_native_reading(kb_dir, failed_reprocess):
    first, failed, original, *_ = failed_reprocess

    selected = resolve_query_views(kb_dir)
    assert selected.has_evidence
    view = selected.views[0]
    retained = source_capabilities(view)["operations"]
    assert retained["source_revision_id"] == first.source_revision_id
    assert retained["processing_status"] == "retained_before_reprocessing"
    assert retained["target_source_revision_id"] == failed.source_revision_id
    assert "retained_before_reprocessing" in selection_catalog(selected)
    assert (view.scope.wiki_dir / retained["path"]).read_bytes() == original
    assert "inspection valve" in read_query_page(
        selected, retained["path"], view_id="legacy", strict=True
    )
    assert "summaries/operations.md" not in view.files


@pytest.mark.parametrize("action", ["withdraw", "empty", "replace_original"])
def test_retained_reading_never_restores_deliberately_inactive_sources(
    kb_dir, failed_reprocess, action
):
    from openkb.application.refresh import confirm_empty_source
    from openkb.application.removal import remove_document
    from openkb.source_catalog import read_source

    first, _, _, path, *_ = failed_reprocess
    if action == "withdraw":
        assert remove_document(kb_dir, first.source_id).status == "removed"
    elif action == "empty":
        source = read_source(kb_dir, first.source_id)
        confirm_empty_source(kb_dir, first.source_id, generation=source.target_generation)
    else:
        write_original(path, "Replacement: the valve stays shut after inspection.")
        assert import_document(kb_dir, path).status == "failed"
    assert not resolve_query_views(kb_dir).has_evidence


@pytest.mark.parametrize(
    "damage",
    ["cycle", "unrelated_parent", "source_identity", "format", "assets", "missing", "tamper"],
)
def test_retained_reading_rejects_unproven_reprocessing_identity(kb_dir, failed_reprocess, damage):
    import json

    from openkb.source_catalog import read_source_revision

    _, failed, *_ = failed_reprocess
    revision = read_source_revision(kb_dir, failed.source_revision_id)
    path = kb_dir / ".openkb/catalog/source-revisions" / f"{failed.source_revision_id}.json"
    raw = json.loads(path.read_text())
    if damage == "cycle":
        raw["reprocessed_from"] = failed.source_revision_id
    elif damage == "unrelated_parent":
        raw["reprocessed_from"] = None
    elif damage == "source_identity":
        raw["source_id"] = "f" * 32
    elif damage == "format":
        raw["source_format"] = "md" if revision.source_format == "pdf" else "pdf"
    elif damage == "assets":
        raw["assets"] = [{"original_reference": "missing.png", "artifact": None, "digest": None}]
    elif damage == "missing":
        (kb_dir / revision.original).unlink()
    else:
        (kb_dir / revision.original).write_text("Tampered frozen bytes")
    path.write_text(json.dumps(raw))  # Deliberately corrupt a temporary fixture record.
    assert not resolve_query_views(kb_dir).has_evidence


def test_resume_updates_native_status_but_keeps_unchanged_summary_pending(
    kb_dir, failed_reprocess, monkeypatch
):
    import litellm

    from openkb.application.views import view_scope

    first, _, _, _, preview, completion, acompletion = failed_reprocess
    old_head = read_head(kb_dir, "legacy").knowledge_revision_id
    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    completed = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert completed.status == "added", completed.message
    selection = resolve_query_views(kb_dir)
    source = source_capabilities(selection.views[0])["operations"]
    assert source["source_revision_id"] == completed.source_revision_id
    assert "processing_status" not in source
    # The fixture produces the exact old summary bytes. Existing publication
    # deliberately preserves its old dependency rather than silently rebinding it.
    assert "summaries/operations.md" in read_head(kb_dir, "legacy").needs_refresh
    assert "summaries/operations.md" not in selection.views[0].files
    historic = resolve_query_views(
        kb_dir, scope=view_scope(kb_dir, "legacy", historical_revision=old_head)
    )
    assert (
        source_capabilities(historic.views[0])["operations"]["source_revision_id"]
        == first.source_revision_id
    )
    assert "summaries/operations.md" in historic.views[0].files


def test_multiple_failed_reprocesses_preserve_the_original_published_revision(
    kb_dir, failed_reprocess
):
    first, previous, original, *_ = failed_reprocess
    preview = preview_reprocessing(kb_dir, first.source_id)
    latest = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert latest.status == "failed"
    assert latest.source_revision_id != previous.source_revision_id
    selected = resolve_query_views(kb_dir)
    source = source_capabilities(selected.views[0])["operations"]
    assert source["source_revision_id"] == first.source_revision_id
    assert source["target_source_revision_id"] == latest.source_revision_id
    assert (selected.views[0].scope.wiki_dir / source["path"]).read_bytes() == original


def test_exact_input_exclusion_still_blocks_same_original_retention(kb_dir, failed_reprocess):
    import json

    first, *_ = failed_reprocess
    path = kb_dir / ".openkb/catalog/sources" / f"{first.source_id}.json"
    record = json.loads(path.read_text())
    head = read_head(kb_dir, "legacy")
    record["excluded_inputs"] = {value: "withdrawn" for value in head.inputs.values()}
    # Exercise exclusion independently of whole-source removal/empty flags.
    assert not record["removed"] and not record["contribution_empty"]
    path.write_text(json.dumps(record))
    assert not resolve_query_views(kb_dir).has_evidence
