"""A failed same-original reprocess must not erase the native reading entry."""

import pytest

from ultrakb.agent.query_evidence import selection_catalog
from ultrakb.application.documents import import_document
from ultrakb.application.query_views import read_query_page, resolve_query_views
from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
from ultrakb.source_reading import source_capabilities
from ultrakb.unit_publication import read_head

pytest_plugins = ("test_office_import", "block_fixtures", "workbook_fixtures")


def write_original(path, text):
    if path.suffix == ".md":
        path.write_text(text)
    else:
        import io

        import pymupdf
        from PIL import Image

        picture = io.BytesIO()
        Image.new("RGB", (64, 64), "navy").save(picture, format="PNG")
        path.unlink(missing_ok=True)
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), text)
            page.insert_image(pymupdf.Rect(72, 96, 120, 144), stream=picture.getvalue())
            pdf.save(path)


def _failed_reprocess(kb_dir, tmp_path, monkeypatch, source_format):
    import litellm

    path = tmp_path / ("operations.md" if source_format == "markdown" else "operations.pdf")
    write_original(path, "The pump must stop before the inspection valve opens.")
    first = publish_retained(kb_dir, path)
    assert first.status == "added", first.message
    head = read_head(kb_dir, "legacy")
    before = resolve_query_views(kb_dir)
    assert "summaries/operations.md" in before.views[0].files
    source = next(iter(source_capabilities(before.views[0]).values()))
    original = (before.views[0].scope.wiki_dir / source["path"]).read_bytes()
    completion, acompletion = litellm.completion, litellm.acompletion

    def rejected(**kwargs):
        raise ValueError("Local delivery boundary rejects summary generation")

    async def arejected(**kwargs):
        return rejected(**kwargs)

    monkeypatch.setattr("litellm.completion", rejected)
    monkeypatch.setattr("litellm.acompletion", arejected)
    preview = preview_reprocessing(kb_dir, first.source_id)
    failed = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert failed.status == "failed", failed.message
    assert read_head(kb_dir, "legacy").knowledge_revision_id == head.knowledge_revision_id
    return first, failed, original, path, preview, completion, acompletion


def publish_retained(kb_dir, path):
    """Publish actual converted source data at the storage boundary, with no LLM."""
    from publication_fixtures import publish_document

    from ultrakb.config import resolve_credential_bundle

    return publish_document(kb_dir, path, None, bundle=resolve_credential_bundle(kb_dir))


@pytest.fixture(params=["markdown", "pdf"])
def failed_reprocess(kb_dir, tmp_path, monkeypatch, pdf_model, request):
    return _failed_reprocess(kb_dir, tmp_path, monkeypatch, request.param)


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


def test_unrelated_publication_keeps_failed_reprocessing_original_readable(
    kb_dir, failed_reprocess, tmp_path, monkeypatch
):
    first, failed, original, _, _, completion, acompletion = failed_reprocess
    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    other = tmp_path / "other.md"
    other.write_text("The unrelated compressor has a separate inspection schedule.")
    assert publish_retained(kb_dir, other).status == "added"

    selected = resolve_query_views(kb_dir)
    view = selected.views[0]
    sources = source_capabilities(view)
    assert set(sources) == {"operations", "other"}
    retained = sources["operations"]
    assert retained["source_revision_id"] == first.source_revision_id
    assert retained["target_source_revision_id"] == failed.source_revision_id
    assert retained["processing_status"] == "retained_before_reprocessing"
    assert (view.scope.wiki_dir / retained["path"]).read_bytes() == original
    assert "inspection valve" in read_query_page(
        selected, retained["path"], view_id="legacy", strict=True
    )
    assert "summaries/operations.md" in read_head(kb_dir).needs_refresh
    assert "summaries/operations.md" not in view.files


@pytest.mark.parametrize("historical", [False, True])
def test_missing_retained_native_file_reads_exact_ancestor_without_repair(
    kb_dir, failed_reprocess, tmp_path, monkeypatch, historical
):
    import asyncio

    from agents import Agent
    from test_answer_evidence_regression import invoke

    from ultrakb.agent.query_evidence import restrict_query_agent
    from ultrakb.application.query_views import read_selected_file
    from ultrakb.application.views import view_scope

    first, failed, original, original_path, _, completion, acompletion = failed_reprocess
    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    other = tmp_path / "other.md"
    other.write_text("The unrelated compressor has a separate inspection schedule.")
    assert publish_retained(kb_dir, other).status == "added"
    current = resolve_query_views(kb_dir).views[0]
    original_images = {
        name: read_selected_file(current, name)
        for name in current.files
        if name.startswith("sources/images/operations/")
    }
    if original_path.suffix == ".pdf":
        assert original_images
    # Reproduce an old publication that omitted only the retained native files.
    for path in (current.scope.wiki_dir / "sources").glob("operations.*"):
        path.unlink()
    for name in original_images:
        (current.scope.wiki_dir / name).unlink()
    head = read_head(kb_dir)

    scope = (
        view_scope(kb_dir, "legacy", historical_revision=head.knowledge_revision_id)
        if historical
        else None
    )
    selected = resolve_query_views(kb_dir, scope=scope)
    view = selected.views[0]
    retained = source_capabilities(view)["operations"]
    assert retained["source_revision_id"] == first.source_revision_id
    if not historical:
        assert retained["target_source_revision_id"] == failed.source_revision_id
        assert retained["processing_status"] == "retained_before_reprocessing"
    else:
        assert view.validity == "historical"
        assert "processing_status" not in retained
    assert read_selected_file(view, retained["path"]) == original
    assert ("summaries/operations.md" in view.files) is historical
    action = retained["reading_action"]
    agent = restrict_query_agent(Agent(name="retained-native-reader"), selected)
    answer = asyncio.run(invoke(agent, action["tool"], **action["arguments"]))
    assert "inspection valve" in answer
    assert first.source_revision_id in answer
    for name, data in original_images.items():
        assert read_selected_file(view, name) == data
        assert not (view.scope.wiki_dir / name).exists()
    assert read_head(kb_dir) == head
    assert not (view.scope.wiki_dir / retained["path"]).exists()


@pytest.mark.parametrize("damage", ["map", "body", "image", "symlink", "ancestor_after_selection"])
def test_retained_fallback_does_not_hide_tampering(
    kb_dir, failed_reprocess, tmp_path, monkeypatch, damage
):
    from ultrakb.application.query_views import read_selected_file

    _, _, _, path, _, completion, acompletion = failed_reprocess
    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    other = tmp_path / "other.md"
    other.write_text("A separate source remains available.")
    assert publish_retained(kb_dir, other).status == "added"
    view = resolve_query_views(kb_dir).views[0]
    native = source_capabilities(view)["operations"]["path"]
    if damage == "image" and path.suffix != ".pdf":
        # The actual Markdown original has text only. Its retained fallback
        # must not acquire invented image evidence while the PDF case below
        # still verifies that changing real pixels is rejected.
        assert not any(name.startswith("sources/images/operations/") for name in view.files)
        with pytest.raises(ValueError, match="not found"):
            read_selected_file(view, "sources/images/operations/not-present.png")
        return
    target = view.scope.wiki_dir / native
    if damage == "image":
        image = next(n for n in view.files if n.startswith("sources/images/operations/"))
        (view.scope.wiki_dir / image).write_bytes(b"corrupted")
    elif damage == "body":
        (view.scope.wiki_dir / "sources/operations.md").write_bytes(b"corrupted")
    elif damage == "map":
        target.write_bytes(b"corrupted")
    elif damage == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "missing-target")
    else:
        target.unlink()
        selected = resolve_query_views(kb_dir).views[0]
        (selected.native_file_roots[native] / native).write_bytes(b"corrupted")
        with pytest.raises(ValueError, match="changed after selection"):
            read_selected_file(selected, native)
        return
    with pytest.raises(ValueError):
        resolve_query_views(kb_dir)


@pytest.mark.parametrize("historical", [False, True])
def test_retained_native_inventory_excludes_unregistered_same_name_files(
    kb_dir, failed_reprocess, tmp_path, monkeypatch, historical
):
    import json

    from ultrakb.application.query_views import read_selected_file
    from ultrakb.application.views import view_scope

    _, _, _, path, _, completion, acompletion = failed_reprocess
    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    other = tmp_path / "other.md"
    other.write_text("A separate source remains available.")
    assert publish_retained(kb_dir, other).status == "added"
    view = resolve_query_views(kb_dir).views[0]
    native = source_capabilities(view)["operations"]["path"]
    (view.scope.wiki_dir / native).unlink()
    suffix = ".json" if path.suffix == ".md" else ".content.json"
    shadow = "sources/operations" + suffix
    (view.scope.wiki_dir / shadow).write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": "FORGED ORIGINAL", "images": []}])
        if suffix == ".json"
        else (view.scope.wiki_dir / "sources/other.content.json").read_text()
    )
    scope = (
        view_scope(kb_dir, "legacy", historical_revision=view.knowledge_revision_id)
        if historical
        else None
    )
    selected = resolve_query_views(kb_dir, scope=scope).views[0]
    assert shadow not in selected.files
    assert source_capabilities(selected)["operations"]["path"] == native
    with pytest.raises(ValueError, match="not found"):
        read_selected_file(selected, shadow)


@pytest.mark.parametrize("action", ["withdraw", "empty", "replace_original"])
def test_retained_reading_never_restores_deliberately_inactive_sources(
    kb_dir, failed_reprocess, action
):
    from ultrakb.application.refresh import confirm_empty_source
    from ultrakb.application.removal import remove_document
    from ultrakb.source_catalog import read_source

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

    from ultrakb.source_catalog import read_source_revision

    _, failed, *_ = failed_reprocess
    revision = read_source_revision(kb_dir, failed.source_revision_id)
    path = kb_dir / ".ultrakb/catalog/source-revisions" / f"{failed.source_revision_id}.json"
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


def test_resume_updates_native_status_but_keeps_unchanged_summary_pending(kb_dir, failed_reprocess):
    from processing_fixtures import source_processing_context
    from retained_publication_fixtures import publish_processed_source

    from ultrakb.application.views import view_scope

    first, _, _, _, preview, *_ = failed_reprocess
    old_head = read_head(kb_dir, "legacy").knowledge_revision_id
    summary = kb_dir / "wiki/summaries/operations.md"
    before = summary.read_bytes()
    completed = publish_processed_source(kb_dir, first.source_id, preserve_summary=True)
    assert completed.status == "added", completed.message
    assert summary.read_bytes() == before
    head = read_head(kb_dir, "legacy")
    context = source_processing_context()
    resumed = reprocess_source(kb_dir, first.source_id, version=preview["version"], context=context)
    assert resumed.status == "skipped", resumed.message
    assert resumed.source_revision_id == completed.source_revision_id
    assert resumed.units[0].successful_revision_id == completed.units[0].successful_revision_id
    assert read_head(kb_dir, "legacy") == head
    assert context.executor.calls == 0
    selection = resolve_query_views(kb_dir)
    source = source_capabilities(selection.views[0])["operations"]
    assert source["source_revision_id"] == completed.source_revision_id
    assert "processing_status" not in source
    # An unchanged candidate keeps the old dependency. Publishing the new native
    # revision must not silently claim that this old summary was refreshed.
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
    path = kb_dir / ".ultrakb/catalog/sources" / f"{first.source_id}.json"
    record = json.loads(path.read_text())
    head = read_head(kb_dir, "legacy")
    record["excluded_inputs"] = {value: "withdrawn" for value in head.inputs.values()}
    # Exercise exclusion independently of whole-source removal/empty flags.
    assert not record["removed"] and not record["contribution_empty"]
    path.write_text(json.dumps(record))
    assert not resolve_query_views(kb_dir).has_evidence
