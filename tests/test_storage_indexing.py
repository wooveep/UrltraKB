"""The real SDK builds a reusable document tree in the shared main store."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.pages import read_page
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope
from ultrakb.config import load_config, save_config
from ultrakb.storage.index_store import IndexStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def indexing_response(request, number):
    prompt = "\n".join(str(message.get("content", "")) for message in request["messages"])
    if "detect if there is a table of content" in prompt:
        return {"content": json.dumps({"toc_detected": "no"})}
    if "your task is to generate the tree structure of the document" in prompt:
        return {"content": "[]"}
    node_work = next(
        (
            message["content"].split("\nNode work:\n", 1)[1]
            for message in request["messages"]
            if isinstance(message.get("content"), str) and "\nNode work:\n" in message["content"]
        ),
        None,
    )
    if node_work:
        return {
            "content": json.dumps(
                {
                    "results": [
                        {"id": node["id"], "summary": "The value is 17."}
                        for node in json.loads(node_work)["nodes"]
                    ]
                }
            )
        }
    if any(
        "CONTENT BLOCK STRUCTURE" in str(message.get("content"))
        or "PHYSICAL SLIDE STRUCTURE" in str(message.get("content"))
        for message in request["messages"]
    ):
        if "\nPrevious structure:\n" in prompt:
            previous, _ = json.JSONDecoder().raw_decode(
                prompt.split("\nPrevious structure:\n", 1)[1]
            )
            if previous:
                return {"content": "[]"}
        return {
            "content": json.dumps(
                [
                    {
                        "structure": "1",
                        "title": "Fixture",
                        "title_origin": "generated",
                        "physical_index": 1,
                        "anchor": {
                            "unit": 1,
                            "part": "body",
                            "range": [0, 16],
                            "excerpt": "The value is 17.",
                        },
                    }
                ]
            )
        }
    original = next(
        (
            message["content"]
            for message in request["messages"]
            if isinstance(message.get("content"), str)
            and message["content"].startswith("Original document: ")
        ),
        None,
    )
    if original:
        sections = json.loads(original.split("\n", 1)[1])["output_sections"]
        return {
            "content": json.dumps(
                {
                    "description": "Fixture",
                    "sections": [
                        {
                            "id": section["id"],
                            "parts": [{"kind": "prose", "text": "The value is 17.", "refs": []}],
                        }
                        for section in sections
                    ],
                }
            )
        }
    if request.get("response_format"):
        return {"content": json.dumps({"concepts": [], "entities": []})}
    return {"content": "The value is 17."}


@pytest.mark.asyncio
async def test_long_compile_builds_shared_index_and_reuses_it_after_publication_failure(
    tmp_path, monkeypatch
):
    root, identity, unit = compile_input(
        tmp_path, segmented=True, body_text="# Fixture\nThe value is 17.\n"
    )
    save_config(
        root / ".ultrakb/config.yaml",
        {
            **load_config(root / ".ultrakb/config.yaml"),
            "model_policy": {"max_calls": 10, "retries": 0},
        },
    )
    context = ExecutionContext()

    def interrupted_publish(*args, **options):
        raise OSError("publication interrupted after indexing")

    with StreamTransport(indexing_response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:
            failure.setattr(KnowledgeStore, "publish", interrupted_publish)
            result = await recompile_document(
                root,
                unit.source_id,
                unit_id=unit.unit_id,
                context=context,
                scope=storage_scope(root, identity),
            )
        assert result.status == "blocked", result.message
        sent = len(service.requests)
        assert sent >= 4
        resumed = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert resumed.status == "compiled", resumed.message
        assert len(service.requests) == sent
    bound = KnowledgeStore(root, identity).bind()
    [index] = bound.index_revisions()
    document = IndexStore(root, identity).document(index)
    assert document["structure"][0]["text"] == "# Fixture\nThe value is 17."
    assert "17" in read_page(root, "summaries/fixture.md", scope=storage_scope(root, identity)).body
    assert not list((root / ".ultrakb").glob("**/context.sqlite"))
    assert not (root / ".ultrakb/index-stages").exists()


@pytest.mark.asyncio
async def test_segmented_native_text_uses_its_frozen_block_coordinates(tmp_path):
    from figure_fixtures import record_markdown
    from test_storage_questions import question_knowledge

    from ultrakb.block_package import freeze_block_package
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore

    root, identity, _ = question_knowledge(tmp_path)
    directory = tmp_path / "native"
    record_markdown(directory, "sources/fixture.md", "The value is 17.\n")
    body = directory / "sources/fixture.md"
    freeze_block_package(body, "fixture")
    native = body.with_suffix(".content.json")
    catalog = CatalogStore(root, identity)
    source = catalog.admit(body, source_key="fixture")
    processing = catalog.retain_processing(
        source.revision_id,
        {"fixture.md": body, "fixture.content.json": native},
        body_path="fixture.md",
        source_path="fixture.content.json",
        source_kind="text",
        fingerprint="native-block-v1",
        operation_id="native-block",
        decision=classify_markdown_tokens(10).model_copy(update={"execution_mode": "segmented"}),
    )
    unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="fixture")
    body.unlink()
    native.unlink()
    save_config(
        root / ".ultrakb/config.yaml",
        {
            **load_config(root / ".ultrakb/config.yaml"),
            "model_policy": {"max_calls": 10, "retries": 0},
        },
    )
    with StreamTransport(indexing_response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=storage_scope(root, identity)
        )
        assert result.status == "compiled", result.message
    bound = KnowledgeStore(root, identity).bind()
    [index] = bound.index_revisions()
    document = IndexStore(root, identity).document(index)
    assert document["metadata"]["source"]["text"] == "The value is 17.\n"
    assert document["pages"][0]["source_spans"] == [[0, 17]]
    assert document["structure"][0]["anchor"]["range"] == [0, 16]


@pytest.mark.asyncio
@pytest.mark.parametrize("notes", [False, True], ids=["pdf-pages", "slide-notes"])
async def test_segmented_native_pages_keep_physical_body_and_notes_coordinates(tmp_path, notes):
    from test_storage_questions import question_knowledge

    from ultrakb.locks import atomic_write_json
    from ultrakb.office.slide_content import combine_parts
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.source_contract import NATIVE_PAGE_POLICY
    from ultrakb.storage.catalog_store import CatalogStore

    root, identity, _ = question_knowledge(tmp_path)
    body = tmp_path / "fixture.md"
    body.write_text("The value is 17.")
    pages = [
        {
            "page": 1,
            "unit_kind": "page",
            "content": combine_parts("The value is 17.", "Retained original speaker notes."),
            "parts": {"body": "The value is 17.", "notes": "Retained original speaker notes."},
            "source_structure_policy": NATIVE_PAGE_POLICY,
            "content_spans": [],
            "images": [],
            "image_reference_spans": [],
        }
    ]
    if not notes:
        pages[0].pop("parts")
        pages[0]["content"] = "The value is 17."
    native = tmp_path / "fixture.json"
    atomic_write_json(native, pages)
    catalog = CatalogStore(root, identity)
    source = catalog.admit(body, source_key="fixture")
    processing = catalog.retain_processing(
        source.revision_id,
        {"fixture.md": body, "fixture.json": native},
        body_path="fixture.md",
        source_path="fixture.json",
        source_kind="pages",
        fingerprint="native-pages-v1",
        operation_id="native-pages",
        decision=classify_markdown_tokens(10).model_copy(update={"execution_mode": "segmented"}),
    )
    unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="fixture")
    body.unlink()
    native.unlink()
    save_config(
        root / ".ultrakb/config.yaml",
        {
            **load_config(root / ".ultrakb/config.yaml"),
            "model_policy": {"max_calls": 10, "retries": 0},
        },
    )
    with StreamTransport(indexing_response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=storage_scope(root, identity)
        )
        assert result.status == "compiled", result.message
    bound = KnowledgeStore(root, identity).bind()
    [index] = bound.index_revisions()
    document = IndexStore(root, identity).document(index)
    assert document["pages"] == pages
    if notes:
        assert document["metadata"]["page_parts"] == [pages[0]["parts"]]
        assert document["structure"][0]["anchor"] == {
            "unit": 1,
            "part": "body",
            "range": [0, 16],
            "excerpt": "The value is 17.",
        }
    else:
        assert document["structure"][0]["start_index"] == 1
        assert document["structure"][0]["end_index"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_nodes", [False, True], ids=["raw-response", "accepted-node"])
async def test_received_index_response_recovers_from_gateway_receipt_without_resending(
    tmp_path, monkeypatch, partial_nodes
):
    from ultrakb.storage.index_checkpoints import StoredIndexStages

    root, identity, unit = compile_input(
        tmp_path,
        segmented=True,
        body_text=(
            "# Fixture\nThe value is 17.\n# Additional\nThe result is retained.\n"
            if partial_nodes
            else "# Fixture\nThe value is 17.\n"
        ),
    )
    save_config(
        root / ".ultrakb/config.yaml",
        {
            **load_config(root / ".ultrakb/config.yaml"),
            "model_policy": {"max_calls": 10, "retries": 0},
        },
    )
    context, save, accepted = ExecutionContext(), StoredIndexStages.save, []

    def lost_delivery(self, key, value):
        if key.startswith("stage:node-work:"):
            accepted.append(key)
        if (not partial_nodes and key.startswith("response:")) or (
            partial_nodes and key.startswith("stage:node-work:") and len(accepted) == 2
        ):
            raise OSError("index response acknowledgement interrupted")
        return save(self, key, value)

    with StreamTransport(indexing_response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:
            failure.setattr(StoredIndexStages, "save", lost_delivery)
            result = await recompile_document(
                root,
                unit.source_id,
                unit_id=unit.unit_id,
                context=context,
                scope=storage_scope(root, identity),
            )
        assert result.status == "blocked"
        assert len(service.requests) == 1
        first_request = service.requests[0]["messages"]
        resumed = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert resumed.status == "compiled", resumed.message
        assert len(service.requests) == 4
        assert sum(request["messages"] == first_request for request in service.requests) == 1
