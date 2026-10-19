"""Public document admission feeds the shared compiler and immutable publications."""

import asyncio
import json

import pytest
from figure_fixtures import png_bytes
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.pages import read_page
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("confirmation", ["processing", "publication"])
def test_published_import_keeps_its_result_when_followup_reads_fail(
    tmp_path, monkeypatch, confirmation
):
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "guide.md"
    source.write_text("The value is 17.")
    published = False
    publish = KnowledgeStore.publish
    processing, bind = CatalogStore.processing, KnowledgeStore.bind

    def publish_then_lose_reads(*args, **options):
        nonlocal published
        result = publish(*args, **options)
        published = True
        return result

    def read_processing(*args, **options):
        if published and confirmation == "processing":
            raise OSError("Processing confirmation unavailable after publication")
        return processing(*args, **options)

    def read_publication(*args, **options):
        if published and confirmation == "publication":
            raise OSError("Publication confirmation unavailable after publication")
        return bind(*args, **options)

    monkeypatch.setattr(KnowledgeStore, "publish", publish_then_lose_reads)
    monkeypatch.setattr(CatalogStore, "processing", read_processing)
    monkeypatch.setattr(KnowledgeStore, "bind", read_publication)
    summary = json.dumps({"description": "Guide", "content": "The value is 17."})
    plan = json.dumps({"concepts": [], "entities": []})
    with StreamTransport([{"content": summary}, {"content": plan}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, source, scope=storage_scope(root, identity))
        assert published
        assert result.status == "added", result.message
        [unit] = result.units
        assert unit.status == "completed"
        assert unit.knowledge_revision_id is not None
        assert len(service.requests) == 2
    monkeypatch.undo()
    current = storage_scope(root, identity)
    assert current.knowledge.publication.revision_id == unit.knowledge_revision_id
    assert "17" in read_page(root, f"summaries/{unit.doc_name}.md", scope=current).body


def test_explicit_import_credentials_are_frozen_and_used_for_the_actual_sends(tmp_path):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.config import LlmCredentialBundle

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "guide.md"
    source.write_text("The value is 17.")
    summary = json.dumps({"description": "Guide", "content": "The value is 17."})
    plan = json.dumps({"concepts": [], "entities": []})
    responses = [{"content": summary}, {"content": plan}]
    context = ExecutionContext()
    with StreamTransport(responses) as configured, StreamTransport(responses) as explicit:
        (root / ".env").write_text(
            f"LLM_API_KEY=configured-key\nOPENAI_API_BASE={configured.url}\n"
        )
        bundle = LlmCredentialBundle(api_key="explicit-key", base_url=explicit.url)
        result = import_document(
            root, source, bundle=bundle, context=context, scope=storage_scope(root, identity)
        )
        assert result.status == "added", result.message
        assert configured.requests == []
        assert len(explicit.requests) == 2
        assert context.snapshot.values()["credentials"]["base_url"] == explicit.url
        assert context.executor.bindings.credentials() == bundle


def test_public_import_retains_markdown_sidecars_and_recompiles_without_external_files(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    source = external / "guide.md"
    source.write_text("# Guide\nThe value is 17.\n\n![Original](picture.png)\n")
    picture = external / "picture.png"
    picture.write_bytes(png_bytes("red"))
    summary = json.dumps({"description": "Guide", "content": "The value is 17."})
    plan = json.dumps({"concepts": [], "entities": []})
    with StreamTransport(
        [{"content": summary}, {"content": plan}, {"content": summary}, {"content": plan}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        imported = import_document(root, source, scope=storage_scope(root, identity))
        assert imported.status == "added", imported.message
        [unit] = imported.units
        assert unit.status == "completed"
        repeated = import_document(root, source, scope=storage_scope(root, identity))
        assert repeated.status == "skipped", repeated.message
        assert len(service.requests) == 2
        source.unlink()
        picture.unlink()
        recompiled = asyncio.run(
            recompile_document(
                root,
                imported.source_id,
                unit_id=unit.unit_id,
                scope=storage_scope(root, identity),
            )
        )
        assert recompiled.status == "compiled", recompiled.message
    page = read_page(root, f"summaries/{unit.doc_name}.md", scope=storage_scope(root, identity))
    assert "The value is 17." in page.body
    assert "figure-assets/" in page.body
    for name in ("catalog", "normalized", "hashes.json", "model-tasks", "index-stages"):
        assert not (root / ".ultrakb" / name).exists()


@pytest.mark.parametrize("segmented", [False, True], ids=["full", "segmented"])
def test_public_import_retains_native_pdf_pages_without_private_index_files(tmp_path, segmented):
    import pymupdf
    from test_storage_indexing import indexing_response

    from ultrakb.config import load_config, save_config
    from ultrakb.storage.index_store import IndexStore
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "original.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "The value is 17.")
    document.save(source)
    document.close()
    save_config(
        root / ".ultrakb/config.yaml",
        {
            **load_config(root / ".ultrakb/config.yaml"),
            "pdf_short_max_pages": 0 if segmented else 10,
            "model_policy": {"max_calls": 10, "retries": 0},
        },
    )

    def response(request, number):
        if request.get("response_format") and not any(
            str(message.get("content", "")).startswith("Original document: ")
            for message in request["messages"]
        ):
            return {
                "content": json.dumps({"description": "Fixture", "content": "The value is 17."})
                if number == 0 and not segmented
                else json.dumps({"concepts": [], "entities": []})
            }
        return indexing_response(request, number)

    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        imported = import_document(root, source, scope=storage_scope(root, identity))
        assert imported.status == "added", imported.message
        [unit] = imported.units
        assert unit.execution_mode == ("segmented" if segmented else "full")
    source.unlink()
    scope = storage_scope(root, identity)
    native = json.loads(scope.knowledge.read(f"sources/{unit.doc_name}.json"))
    assert native[0]["page"] == 1
    assert native[0]["content"] == "The value is 17."
    if segmented:
        [index] = KnowledgeStore(root, identity).bind().index_revisions()
        assert IndexStore(root, identity).document(index)["pages"] == native
    assert "17" in read_page(root, f"summaries/{unit.doc_name}.md", scope=scope).body
    assert not list((root / ".ultrakb").glob("**/context.sqlite"))


def test_failed_normalization_keeps_the_admitted_source_identity_and_original(
    tmp_path, monkeypatch
):
    from ultrakb.storage.catalog_store import CatalogStore

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "original.md"
    source.write_text("The value is 17.")

    def interrupted_retention(*args, **options):
        raise OSError("normalization confirmation interrupted")

    monkeypatch.setattr(CatalogStore, "retain_processing", interrupted_retention)
    result = import_document(root, source, scope=storage_scope(root, identity))
    assert result.status == "blocked"
    assert result.source_id is not None
    assert result.source_revision_id is not None
    source.unlink()
    assert (
        CatalogStore(root, identity).read_original(result.source_revision_id) == b"The value is 17."
    )


def test_public_long_text_import_keeps_blocks_without_building_an_unused_package(
    tmp_path, monkeypatch
):
    from test_storage_indexing import indexing_response

    from ultrakb import block_package
    from ultrakb.config import load_config, save_config
    from ultrakb.storage.index_store import IndexStore
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "long.txt"
    original = "The value is 17.\n" + "Further details are retained.\n" * 1500
    source.write_text(original)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            **load_config(root / ".ultrakb/config.yaml"),
            "model_policy": {"max_calls": 10, "retries": 0},
        },
    )
    write = block_package.atomic_write_text

    def no_unused_package(path, text, **options):
        if path.suffix == ".okbi":
            raise OSError("An unused input package must not be a runtime dependency")
        return write(path, text, **options)

    monkeypatch.setattr(block_package, "atomic_write_text", no_unused_package)
    with StreamTransport(indexing_response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = import_document(root, source, scope=storage_scope(root, identity))
        assert result.status == "added", result.message
    [unit] = result.units
    assert unit.length_class == "long"
    assert unit.execution_mode == "segmented"
    [index] = KnowledgeStore(root, identity).bind().index_revisions()
    document = IndexStore(root, identity).document(index)
    assert document["metadata"]["source"]["text"] == original
    assert document["metadata"]["source"]["blocks"]
    assert not list((root / ".ultrakb").glob("**/context.sqlite"))
