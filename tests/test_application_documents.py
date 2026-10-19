"""Real document conversion, model generation, and durable import boundaries."""

import pytest
from application_llm_fixtures import real_application_model  # noqa: F401

from ultrakb.application.pages import read_page
from ultrakb.application.views import view_scope
from ultrakb.documents import read_document_source

pytest_plugins = ("processing_fixtures",)


def test_import_document_compiles_and_deduplicates(kb_dir, tmp_path, monkeypatch):
    from ultrakb.application.documents import import_document
    from ultrakb.application.knowledge_bases import get_kb_list

    source = tmp_path / "notes.md"
    source.write_text("# Notes\nThe recorded coolant limit is 17 degrees.")
    events = []
    result = import_document(kb_dir, source, on_event=events.append)
    assert result.status == "added"
    scope = view_scope(kb_dir, result.units[0].view_id)
    body = read_page(kb_dir, "summaries/notes", scope=scope).body
    assert "17" in body and "coolant" in body.lower()
    assert get_kb_list(kb_dir)["document_count"] == 1
    assert import_document(kb_dir, source).status == "skipped"
    assert [event["stage"] for event in events if "stage" in event] == [
        "preparing",
        "converting",
        "compiling",
        "committed",
    ]
    usage_events = [event["data"] for event in events if event.get("event") == "model_usage"]
    usage = result.model_usage["current"]
    assert usage_events[-1]["current"] == usage
    assert usage["requests"] >= 2 and usage["total_known"] > 0


def test_import_uses_one_configuration_snapshot_across_model_calls(kb_dir, monkeypatch, live_llm):
    import litellm

    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.config import resolve_effective_config

    configuration = kb_dir / ".ultrakb/config.yaml"
    configuration.write_text(
        "model: deepseek/deepseek-flash\nlanguage: en\nextra_headers:\n  X-Profile: initial\n"
    )
    initial_key = live_llm.credentials.api_key
    source = kb_dir / "notes.md"
    source.write_text("# Notes\nOriginal knowledge.")
    calls = []

    genuine_completion = litellm.completion

    def completion(**kwargs):
        calls.append(kwargs)
        # The operation must keep the settings captured before the external edit.
        configuration.write_text("model: openai/changed\nlanguage: zh\n")
        (kb_dir / ".env").write_text("LLM_API_KEY=changed-private-key\n")
        return genuine_completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    context = ExecutionContext()
    result = import_document(kb_dir, source, context=context)
    assert result.status == "added"
    assert len(calls) >= 2
    assert all(call["model"] == "deepseek/deepseek-flash" for call in calls)
    assert all(call["api_key"] == initial_key for call in calls)
    assert all(call["extra_headers"]["X-Profile"] == "initial" for call in calls)
    assert context.snapshot is not None
    key_is_redacted = initial_key not in repr(context) and initial_key not in repr(context.snapshot)
    assert key_is_redacted
    assert resolve_effective_config(kb_dir)[0]["model"] == "openai/changed"


def _first_image(kb_dir, retained, source):
    from ultrakb.application.storage_sources import read_source_image
    from ultrakb.application.views import published_scope

    return read_source_image(
        kb_dir,
        retained.source_id,
        retained.unit_revision_id,
        next(iter(source["assets"])),
        identity=published_scope(kb_dir).knowledge.identity,
    )[0]


def _retained_original(kb_dir, source):
    from ultrakb.application.views import published_scope
    from ultrakb.storage.catalog_store import CatalogStore

    return CatalogStore(kb_dir, published_scope(kb_dir).knowledge.identity).read_original(
        source.source_revision_id
    )


@pytest.mark.integration
@pytest.mark.parametrize("extension", [".md", ".markdown"])
def test_import_freezes_relative_images_at_the_business_boundary(
    kb_dir, tmp_path, monkeypatch, extension, processing_only
):
    from figure_fixtures import png_bytes
    from processing_fixtures import source_processing_context

    source = tmp_path / f"中文 图解{extension}"
    figure = tmp_path / "图.png"
    picture = png_bytes()
    figure.write_bytes(picture)
    source.write_text("# 图解\n![first](图.png)\n![again](图.png)\n![missing](later.png)")
    original = source.read_bytes()

    from ultrakb.storage.catalog_store import CatalogStore

    admit = CatalogStore.admit

    def after_freeze(store, *args, **kwargs):
        source.write_text("changed source")
        figure.write_bytes(png_bytes("blue"))
        (tmp_path / "later.png").write_bytes(png_bytes("green"))
        return admit(store, *args, **kwargs)

    monkeypatch.setattr(CatalogStore, "admit", after_freeze)
    result = processing_only(source, context=source_processing_context())
    saved = read_document_source(kb_dir, result.source_id, unit_revision_id=result.unit_revision_id)
    assert _retained_original(kb_dir, result) == original
    assert len(saved["assets"]) == 1
    assert _first_image(kb_dir, result, saved) == picture
    converted = saved["content"]
    assert converted.count("%E5%9B%BE.png)") == 2 and "原图未保存" in converted


@pytest.mark.integration
def test_import_keeps_frozen_images_when_external_resources_change_before_admission(
    kb_dir, tmp_path, monkeypatch, processing_only
):
    from figure_fixtures import png_bytes

    from ultrakb.storage.catalog_store import CatalogStore

    source = tmp_path / "notes.md"
    source.write_text("Recorded setup.\n![figure](figure.png)")
    figure = tmp_path / "figure.png"
    figure.write_bytes(png_bytes())
    admit = CatalogStore.admit

    def changing_resource(store, *args, **kwargs):
        figure.write_bytes(png_bytes("blue"))
        return admit(store, *args, **kwargs)

    monkeypatch.setattr(CatalogStore, "admit", changing_resource)
    result = processing_only(source)
    saved = read_document_source(kb_dir, result.source_id, unit_revision_id=result.unit_revision_id)
    assert _first_image(kb_dir, result, saved) == png_bytes()
    assert figure.read_bytes() == png_bytes("blue")


def test_watched_source_replaced_with_external_symlink_while_waiting_never_starts(kb_dir, tmp_path):
    import os
    import threading

    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import kb_ingest_lock

    if os.name == "nt":
        pytest.skip("POSIX symlink fixture")
    source = kb_dir / "raw/notes.md"
    source.write_text("approved input")
    outside = tmp_path / "outside.md"
    outside.write_text("outside raw")
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            acquired.set()
            assert release.wait(10)

    def waiting(event):
        if event.get("stage") == "waiting":
            source.unlink()
            source.symlink_to(outside)
            release.set()

    context = ExecutionContext(on_event=waiting)
    holder = threading.Thread(target=hold)
    holder.start()
    assert acquired.wait(5)
    try:
        with pytest.raises(ValueError, match="Watched input"):
            import_document(kb_dir, source, source_root=kb_dir / "raw", context=context)
    finally:
        release.set()
        holder.join(5)
    assert context.snapshot is None
    assert outside.read_text() == "outside raw"
    assert not list((kb_dir / "wiki/summaries").iterdir())


@pytest.mark.integration
def test_import_keeps_frozen_identity_when_original_path_changes_after_start(
    kb_dir, monkeypatch, processing_only
):
    import os

    from processing_fixtures import source_processing_context

    from ultrakb.application.views import published_scope
    from ultrakb.storage.catalog_store import CatalogStore

    if os.name == "nt":
        pytest.skip("POSIX symlink fixture")
    source = kb_dir / "notes.md"
    source.write_text("# Original prepared input")
    other = kb_dir / "other.md"
    other.write_text("# Another document")
    processing_only(other)
    summary = kb_dir / "wiki/summaries/other.md"
    summary.write_text("# Keep this unrelated page")

    admit = CatalogStore.admit

    def after_freeze(store, *args, **kwargs):
        source.unlink()
        source.symlink_to(other)
        return admit(store, *args, **kwargs)

    monkeypatch.setattr(CatalogStore, "admit", after_freeze)
    result = processing_only(source, context=source_processing_context())

    saved = read_document_source(kb_dir, result.source_id, unit_revision_id=result.unit_revision_id)
    catalog = CatalogStore(kb_dir, published_scope(kb_dir).knowledge.identity)
    assert catalog.source(result.source_id).source_key.endswith("notes.md")
    assert saved["doc_name"] == result.doc_name and result.doc_name.startswith("notes-")
    assert _retained_original(kb_dir, result).decode() == "# Original prepared input"
    assert summary.read_text() == "# Keep this unrelated page"
