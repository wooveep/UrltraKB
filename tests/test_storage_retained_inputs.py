"""No reliable native text retains originals/figures without replacing knowledge."""

import asyncio
import json

import pytest
from figure_fixtures import png_bytes
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.reading import read_knowledge_image
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.source_retry import retry_source
from ultrakb.application.sources import read_admitted_source
from ultrakb.application.storage_sources import (
    read_retained_asset,
    read_retained_input,
    read_retained_original,
)
from ultrakb.application.views import storage_scope
from ultrakb.documents import read_document_source
from ultrakb.storage.catalog_store import CatalogStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "kind", ["blank_pdf", "image_pdf", "image_markdown", "blank_text", "opaque_json"]
)
def test_unreadable_inputs_remain_managed_and_readable_without_model_work(tmp_path, kind):
    root, identity, _ = question_knowledge(tmp_path)
    if kind.endswith("pdf"):
        import pymupdf

        source = tmp_path / "retained.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            if kind == "image_pdf":
                page.insert_image(pymupdf.Rect(0, 0, 100, 100), stream=png_bytes())
            pdf.save(source)
    elif kind == "image_markdown":
        source = tmp_path / "retained.md"
        (tmp_path / "figure.png").write_bytes(png_bytes())
        source.write_text("![Figure](figure.png)\n")
    elif kind == "opaque_json":
        source = tmp_path / "retained.json"
        source.write_text("[]")
    else:
        source = tmp_path / "retained.txt"
        source.write_text(" \n\t")
    original = source.read_bytes()
    before = storage_scope(root, identity)
    context = ExecutionContext(storage_identity=identity)
    with StreamTransport([{"content": "Unexpected model request"}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, source, scope=before, context=context)
        assert result.status == "blocked", result.message
        assert result.source_id is not None
        assert len(result.units) == 1
        unit = result.units[0]
        assert unit.knowledge_revision_id is None
        assert storage_scope(root, identity).publication_id == before.publication_id
        source.unlink()
        catalog = CatalogStore(root, identity)
        assert catalog.read_original(result.source_revision_id) == original
        target = catalog.unit(unit.unit_id)
        processing = catalog.processing(target.processing_id)
        state = "empty" if kind.startswith("blank") else "unreadable"
        assert processing.content_state == state
        assert read_admitted_source(root, result.source_id, scope=before) is None
        assert read_document_source(root, result.source_id, scope=before) is None
        visible = read_retained_input(
            root, result.source_id, unit.target_revision_id, identity=identity
        )
        assert visible["source_revision_id"] == result.source_revision_id
        assert visible["content_state"] == state
        assert visible["status"] == "retained_without_knowledge"
        assert visible["knowledge_revision_id"] is None
        assert visible["retention"]["observed_blank"] == kind.startswith("blank")
        if kind.startswith("image"):
            assert visible["images"]
            for image in visible["images"]:
                assert read_retained_asset(
                    root,
                    result.source_id,
                    unit.target_revision_id,
                    image["path"],
                    identity=identity,
                )
        assert (
            read_retained_original(
                root, result.source_id, unit.target_revision_id, identity=identity
            )
            == original
        )
        rejected = asyncio.run(
            recompile_document(root, result.source_id, unit_id=unit.unit_id, scope=before)
        )
        assert rejected.status == "blocked"
        retried = retry_source(root, result.source_id, scope=before)
        assert retried.status == "blocked"
        assert retried.units[0].target_revision_id == unit.target_revision_id
        assert not service.requests
        assert result.model_usage["current"]["actual_sends"] == 0
    assert not (root / ".ultrakb/catalog").exists()


def test_retained_replacement_keeps_public_body_and_explicit_historical_input_separate(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "input.md"
    figure = tmp_path / "figure.png"
    figure.write_bytes(png_bytes())
    original = "The value is 17.\n![Figure](figure.png)\n"
    source.write_text(original)
    responses = [
        {"content": json.dumps({"description": "Value", "content": "The value is 17."})},
        {"content": json.dumps({"concepts": [], "entities": []})},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = import_document(root, source, scope=storage_scope(root, identity))
        assert first.status == "added"
        public = storage_scope(root, identity)
        source.write_text("![Figure](figure.png)\n")
        replacement = import_document(root, source, scope=public)
        assert replacement.status == "blocked"
        assert replacement.source_id == first.source_id
        assert replacement.units[0].unit_id == first.units[0].unit_id
        assert storage_scope(root, identity).publication_id == public.publication_id
        assert "17" in read_document_source(root, first.source_id, scope=public)["content"]
        assert (
            read_document_source(
                root,
                first.source_id,
                scope=public,
                source_revision_id=replacement.source_revision_id,
            )
            is None
        )
        target = replacement.units[0].target_revision_id
        diagnostic = read_retained_input(root, replacement.source_id, target, identity=identity)
        assert diagnostic["publication_id"] is None
        assert diagnostic["source_revision_id"] == replacement.source_revision_id
        assert "17" not in diagnostic["content"]
        with pytest.raises(ValueError, match="another source"):
            read_retained_input(root, "0" * 32, target, identity=identity)
        with pytest.raises(FileNotFoundError):
            read_retained_asset(
                root, replacement.source_id, target, "../escape.png", identity=identity
            )
        source.write_text("![Figure](figure.png)\n\n")
        # Updating the current target cannot change an explicit retained-input reference.
        changed = import_document(root, source, scope=public)
        assert changed.source_revision_id != replacement.source_revision_id
        assert (
            read_retained_input(root, replacement.source_id, target, identity=identity)["content"]
            == diagnostic["content"]
        )
        assert len(service.requests) == 2


def test_stopping_native_retention_preserves_only_admitted_original(tmp_path):
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "blank.txt"
    source.write_text(" \n")
    stopped = False
    executions = ExecutionStore(root, identity)

    def on_event(event):
        nonlocal stopped
        if (
            event.get("event") == "activity"
            and event["data"]["stage"] == "import.retain_original.v1"
            and event["data"]["status"] == "started"
        ):
            executions.request_stop(context.storage_execution.execution_id)
            stopped = True

    with StreamTransport(
        [
            {"content": json.dumps({"description": "Invalid", "content": "Invented content."})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        context = ExecutionContext(cancelled=lambda: stopped, on_event=on_event)
        public = storage_scope(root, identity)
        result = import_document(root, source, scope=public, context=context)
        assert result.status == "stopped", result.message
        assert result.source_id
        catalog = CatalogStore(root, identity)
        assert catalog.read_original(result.source_revision_id) == b" \n"
        assert catalog.unit_for_source(result.source_id, key="body") is None
        assert executions.status(context.storage_execution.execution_id) == "cancelled"
        source.unlink()
        retried = retry_source(root, result.source_id, scope=public)
        assert retried.status == "blocked", retried.message
        target = catalog.unit(retried.units[0].unit_id)
        assert catalog.processing(target.processing_id).content_state == "empty"
        assert storage_scope(root, identity).publication_id == public.publication_id
        assert not service.requests


@pytest.mark.parametrize("source_format", ["markdown", "pdf"])
def test_public_source_history_keeps_its_native_images_after_new_input(tmp_path, source_format):
    import io

    from PIL import Image

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / ("input.md" if source_format == "markdown" else "input.pdf")
    first_image = png_bytes()
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (20, 130, 230)).save(buffer, format="PNG")
    second_image = buffer.getvalue()

    def write_source(number, image):
        text = f"The value is {number}. A figure shows the recorded input."
        if source_format == "markdown":
            (tmp_path / "figure.png").write_bytes(image)
            source.write_text(text + "\n![Figure](figure.png)\n")
        else:
            import pymupdf

            with pymupdf.open() as pdf:
                page = pdf.new_page()
                page.insert_text((72, 72), text)
                page.insert_image(pymupdf.Rect(72, 100, 172, 200), stream=image)
                pdf.save(source)

    responses = [
        item
        for _ in range(2)
        for item in (
            {
                "content": json.dumps(
                    {"description": "Stable", "content": "A source with a value and a figure."}
                )
            },
            {"content": json.dumps({"concepts": [], "entities": []})},
        )
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        write_source(17, first_image)
        first = import_document(root, source, scope=storage_scope(root, identity))
        assert first.status == "added", first.message
        old = storage_scope(root, identity)
        visible = read_document_source(root, first.source_id, scope=old)
        assert visible["images"]
        original_image = read_knowledge_image(root, visible["images"][0]["path"], scope=old)[0]
        write_source(29, second_image)
        second = import_document(root, source, scope=old)
        assert second.status == "added", second.message
        current = storage_scope(root, identity)
        historical = read_document_source(
            root, first.source_id, scope=current, source_revision_id=first.source_revision_id
        )
        assert "The value is 17." in historical["content"]
        assert "The value is 29." not in historical["content"]
        assert "/retained/" in historical["images"][0]["path"]
        assert (
            read_knowledge_image(root, historical["images"][0]["path"], scope=current)[0]
            == original_image
        )
        if source_format == "pdf":
            assert historical["images"][0]["path"] in historical["content"]
            for image in historical["_qualified_page_images"]:
                assert historical["content"][image["start"] : image["end"]] == image["source"]
            for left, right, ordinal in historical["_page_segments"]:
                page = next(page for page in historical["units"] if page["page"] == ordinal)
                assert historical["content"][left:right] == page["content"]
                assert page["images"][0]["path"] == historical["images"][0]["path"]
        latest = read_document_source(root, first.source_id, scope=current)
        assert "29" in latest["content"]
        assert (
            read_knowledge_image(root, latest["images"][0]["path"], scope=current)[0]
            != original_image
        )
        assert len(service.requests) == 4
