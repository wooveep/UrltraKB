"""Application readers bind CAS resources and page provenance to one publication."""

import pytest


def test_shared_page_image_and_query_readers_keep_the_same_publication(tmp_path):
    from ultrakb.application.pages import read_page
    from ultrakb.application.query_views import (
        read_selected_file,
        resolve_query_views,
        selection_current,
    )
    from ultrakb.application.reading import (
        read_knowledge_context,
        read_knowledge_image,
        read_page_context,
    )
    from ultrakb.application.views import storage_scope
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    first = knowledge.publish(
        knowledge.prepare_revision(
            {
                "index.md": b"Old index",
                "concepts/topic.md": b"# Old topic\n[[other]]",
                "concepts/other.md": b"# Other\n[[topic]]",
                "sources/images/figure.png": b"old image",
            },
            expected=None,
        )
    )
    scope = storage_scope(root, identity)
    selection = resolve_query_views(root, scope=scope)
    knowledge.publish(
        knowledge.prepare_revision(
            {
                "index.md": b"New index",
                "concepts/topic.md": b"# New topic",
                "sources/images/figure.png": b"new image",
            },
            expected=first.publication_id,
        )
    )
    # An unrelated worktree and even an escaped worktree symlink are never authority.
    outside = tmp_path / "unmanaged"
    outside.mkdir()
    (outside / "index.md").write_text("Uncommitted")
    (root / "wiki").symlink_to(outside, target_is_directory=True)
    assert read_page(root, "concepts/topic", scope=scope).body == "# Old topic\n[[other]]"
    assert read_knowledge_image(root, "sources/images/figure.png", scope=scope) == (
        b"old image",
        "image/png",
    )
    assert read_selected_file(selection.views[0], "index.md") == b"Old index"
    context = read_page_context(root, "concepts/topic", scope=scope)
    assert context.page.publication_id == first.publication_id
    assert context.outlinks[0].path == "concepts/other"
    assert context.backlinks[0].path == "concepts/other"
    display = read_knowledge_context(root, "concepts/topic", scope=scope)
    assert {entry.path for entry in display.entries} == {"concepts/topic.md", "concepts/other.md"}
    assert display.context.page == context.page
    assert not selection_current(selection)
    refreshed = storage_scope(root, identity)
    assert read_page(root, "concepts/topic", scope=refreshed).body == "# New topic"


@pytest.mark.parametrize(
    "source_link", ["sources/original.md", "original.md", "summaries/original.md", "original"]
)
def test_a_page_keeps_its_actual_input_after_the_same_unit_is_reprocessed(tmp_path, source_link):
    import json
    from pathlib import Path

    from ultrakb.application.pages import read_page
    from ultrakb.application.query_views import (
        read_query_page,
        read_selected_file,
        resolve_query_views,
    )
    from ultrakb.application.reading import read_knowledge_image, read_page_context
    from ultrakb.application.views import storage_scope
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog = CatalogStore(root, identity)
    original = tmp_path / "original.md"
    original.write_text("Old original\n![figure](images/figure.png)")
    image = tmp_path / "figure.png"
    image.write_bytes(b"Old image")

    def process(operation):
        source = catalog.admit(original, source_key="one-source", sidecars={"figure.png": image})
        processing = catalog.retain_processing(
            source.revision_id,
            {"body.md": original, "images/figure.png": image},
            body_path="body.md",
            fingerprint="policy",
            operation_id=operation,
            decision=classify_markdown_tokens(2),
        )
        unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="original")
        return source, unit

    old_source, old_unit = process("first")
    knowledge = KnowledgeStore(root, identity)
    old_page = f"---\nsources: [{source_link}]\n---\nOld generated claim".encode()
    first = knowledge.publish(
        knowledge.prepare_revision(
            {"concepts/old.md": old_page},
            expected=None,
            unit_revisions=(old_unit.unit_revision_id,),
        )
    )
    original.write_text("New original\n![figure](images/figure.png)")
    image.write_bytes(b"New image")
    new_source, new_unit = process("second")
    knowledge.publish(
        knowledge.prepare_revision(
            {"concepts/old.md": old_page, "concepts/new.md": b"New generated claim"},
            expected=first.publication_id,
            unit_revisions=(new_unit.unit_revision_id,),
            page_inputs={"concepts/new.md": (new_unit.unit_revision_id,)},
        )
    )
    original.unlink()
    scope = storage_scope(root, identity)
    assert read_page(root, "concepts/old", scope=scope).source_revision_ids == (
        old_source.revision_id,
    )
    assert read_page(root, "concepts/new", scope=scope).source_revision_ids == (
        new_source.revision_id,
    )
    assert scope.knowledge.read_original(old_unit.unit_revision_id).startswith(b"Old original")
    assert scope.knowledge.read_source(old_unit.unit_revision_id, "body.md").startswith(
        b"Old original"
    )
    assert scope.knowledge.read_original(new_unit.unit_revision_id).startswith(b"New original")
    context = read_page_context(root, "concepts/old", scope=scope)
    old_source_path = context.sources[0].path
    assert "/retained/" in old_source_path
    assert read_page(root, old_source_path, scope=scope).body.startswith("Old original")
    image_path = (Path(old_source_path).parent / "images/figure.png").as_posix()
    assert read_knowledge_image(root, image_path, scope=scope)[0] == b"Old image"
    selected = resolve_query_views(root, scope=scope)
    rendered = read_query_page(selected, "concepts/old.md", view_id="legacy")
    metadata = json.loads(rendered.split("Reading metadata: ", 1)[1].split("\n\n", 1)[0])
    evidence = metadata["original_sources"][0]
    assert evidence["source_revision_id"] == old_source.revision_id
    assert read_selected_file(selected.views[0], evidence["path"]).startswith(b"Old original")


def test_query_uses_managed_native_sources_and_prepared_model_independent_navigation(
    tmp_path, monkeypatch
):
    from ultrakb.application.query_views import read_selected_file, resolve_query_views
    from ultrakb.application.views import storage_scope
    from ultrakb.context.projection import ensure_projection
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database, database_path
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.tree_store import TreeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog = CatalogStore(root, identity)
    original = tmp_path / "original.md"
    original.write_text("# Native source\nThe recorded value is 17.")
    source = catalog.admit(original, source_key="one")
    processing = catalog.retain_processing(
        source.revision_id,
        {"body.md": original},
        body_path="body.md",
        fingerprint="policy",
        operation_id="one",
        decision=classify_markdown_tokens(12),
    )
    unit = catalog.plan_unit(processing.processing_id, key="one", doc_name="original")
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_revision(
            {"index.md": b"Ready"},
            expected=None,
            unit_revisions=(unit.unit_revision_id,),
        )
    )
    original.unlink()
    path = database_path(root)
    before = path.read_bytes(), path.with_name(path.name + "-wal").read_bytes()

    def no_cold_build(*args, **kwargs):
        raise AssertionError("A public read cannot build navigation")

    monkeypatch.setattr(TreeStore, "build", no_cold_build)
    selected = resolve_query_views(root, scope=storage_scope(root, identity))
    assert selected.has_evidence
    view = selected.views[0]
    assert read_selected_file(view, "sources/original.md").startswith(b"# Native source")
    assert view.source_revisions_by_path["sources/original.md"] == source.revision_id
    first = ensure_projection(selected, {"models": "first"})
    second = ensure_projection(selected, {"models": "second"})
    assert first == second
    assert not (root / ".ultrakb/context/runtime.sqlite").exists()
    assert (path.read_bytes(), path.with_name(path.name + "-wal").read_bytes()) == before


def test_real_knowledge_retrieval_queries_the_bound_shared_tree(tmp_path, monkeypatch):
    import json

    import litellm

    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.views import storage_scope
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.model_tokenizer import TokenMeasure
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog = CatalogStore(root, identity)
    original = tmp_path / "original.md"
    original.write_text("The recorded value is 17, only while the line is cold.")
    source = catalog.admit(original, source_key="one")
    processing = catalog.retain_processing(
        source.revision_id,
        {"body.md": original},
        body_path="body.md",
        fingerprint="policy",
        operation_id="one",
        decision=classify_markdown_tokens(12),
    )
    unit = catalog.plan_unit(processing.processing_id, key="one", doc_name="original")
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_revision(
            {"index.md": b"Ready"},
            expected=None,
            unit_revisions=(unit.unit_revision_id,),
        )
    )
    calls = []

    def rank_provider(**request):
        calls.append(request)
        rank = next(
            tool["function"] for tool in request["tools"] if tool["function"]["name"] == "rank"
        )
        aliases = rank["parameters"]["properties"]["ranked_ids"]["items"]["enum"]
        return litellm.ModelResponse(
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "rank",
                                "type": "function",
                                "function": {
                                    "name": "rank",
                                    "arguments": json.dumps({"ranked_ids": aliases, "done": False}),
                                },
                            }
                        ],
                    },
                }
            ]
        )

    monkeypatch.setattr(litellm, "completion", rank_provider)
    executor = CompletionExecutor(
        RoleBindings("openai/gpt-4o-mini", LlmCredentialBundle(api_key="fixture")),
        ModelCallPolicy(max_calls=10),
    )
    executor.question_capacity = TokenMeasure(
        {
            "unknown_reason": None,
            "input_limit": 65536,
            "measurement_model": "gpt-4o-mini",
        }
    )
    retrieval = KnowledgeRetrieval(
        resolve_query_views(root, scope=storage_scope(root, identity)), executor
    )
    result = retrieval.search("What value applies to the cold line?")
    assert result["status"] == "completed", result
    assert "17" in result["hits"][0]["content"]
    assert "only while the line is cold" in result["hits"][0]["content"]
    assert result["hits"][0]["source_revision_id"] == source.revision_id
    assert calls
    assert not (root / ".ultrakb/context/runtime.sqlite").exists()


def test_reading_scope_does_not_take_ingest_or_hold_an_outer_reclaim_lease(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ultrakb.application.pages import read_page
    from ultrakb.application.views import storage_scope
    from ultrakb.knowledge_scope import reading_scope
    from ultrakb.locks import file_write_lock
    from ultrakb.storage.blobs import put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(knowledge.prepare_revision({"concepts/one.md": b"Committed"}, expected=None))
    scope = storage_scope(root, identity)
    with storage_stage(root, identity) as db:
        put_blob(db, b"An unrelated orphan")
    with reading_scope(scope):
        assert read_page(root, "concepts/one", scope=scope).body == "Committed"
        collected = Maintenance(root, identity).collect()
        assert collected.status != "busy"
        assert collected.deleted_count >= 1
    entered, release = Event(), Event()

    def writer_wait():
        with file_write_lock(root / ".ultrakb/ingest.lock"):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as workers:
        writer = workers.submit(writer_wait)
        try:
            assert entered.wait(5)
            reader = workers.submit(read_page, root, "concepts/one", scope=scope)
            assert reader.result(timeout=2).body == "Committed"
        finally:
            release.set()
        writer.result(timeout=5)


def test_source_resolution_cannot_select_another_publications_input(tmp_path):
    import pytest

    from ultrakb.application.views import storage_scope
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog = CatalogStore(root, identity)
    original = tmp_path / "other.md"
    original.write_text("Not selected")
    source = catalog.admit(original, source_key="other")
    processing = catalog.retain_processing(
        source.revision_id,
        {"body.md": original},
        body_path="body.md",
        fingerprint="policy",
        operation_id="other",
        decision=classify_markdown_tokens(12),
    )
    unit = catalog.plan_unit(processing.processing_id, key="other", doc_name="other")
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_revision({"index.md": b"No selected inputs"}, expected=None)
    )
    scope = storage_scope(root, identity)
    with pytest.raises(ValueError, match="outside this publication"):
        scope.knowledge.read_original(unit.unit_revision_id)
    with pytest.raises(ValueError, match="outside this publication"):
        scope.knowledge.read_source(unit.unit_revision_id, "body.md")


def test_existing_page_context_does_not_scan_unrelated_binary_assets(kb_dir, tmp_path):
    from ultrakb.application.reading import read_page_context
    from ultrakb.locks import atomic_write_text

    atomic_write_text(kb_dir / "wiki/concepts/one.md", "# One\n")
    assets = kb_dir / "wiki/sources/images"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "large.bin").write_bytes(b"An unrelated large asset" * 100_000)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"An unrelated external asset")
    (assets / "external.png").symlink_to(outside)
    assert read_page_context(kb_dir, "concepts/one").page.body == "# One\n"


@pytest.mark.parametrize("failure", ["missing", "identity"])
def test_missing_shared_navigation_returns_the_explicit_recovery_classification(
    tmp_path, monkeypatch, failure
):
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.views import storage_scope
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.context.projection import ensure_projection
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    published = knowledge.publish(knowledge.prepare_revision({"index.md": b"Ready"}, expected=None))
    selected = resolve_query_views(root, scope=storage_scope(root, identity))
    captured = ensure_projection(selected, {})
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "DELETE FROM ukb_revision_navigation WHERE revision_id = ?",
            (published.revision_id,),
        )
    executor = CompletionExecutor(
        RoleBindings("openai/gpt-4o-mini", LlmCredentialBundle(api_key="fixture")),
        ModelCallPolicy(max_calls=0),
    )
    retrieval = KnowledgeRetrieval(selected, executor)
    if failure == "identity":
        knowledge.repair_navigation(published.publication_id)
        # Model the gap after selection while explicit repair changes the tree binding.
        monkeypatch.setattr(retrieval, "ensure_projection", lambda: captured)
    result = retrieval.search("Find the original")
    assert result["status"] == result["reason_code"] == "recovery_required"
    assert result["consumed_sends"] == 0


def test_ambiguous_generated_source_aliases_are_rejected_before_publication(tmp_path):
    import hashlib
    import json

    import pytest

    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.text_source import FrozenText, TextOrigin

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog = CatalogStore(root, identity)
    body = tmp_path / "body.md"
    text = "Seventeen"
    body.write_text(text)
    native = tmp_path / "native.json"
    units = []
    for kind, name in (("text", "manual"), ("pages", "manual.content")):
        native.write_text(
            FrozenText(
                text=text,
                original_characters=len(text),
                original_digest=hashlib.sha256(text.encode()).hexdigest(),
                origins=(TextOrigin(normalized_span=(0, len(text)), original_span=(0, len(text))),),
                assets={},
                tokens=0,
            ).model_dump_json()
            if kind == "text"
            else json.dumps(
                [
                    {"page": 1, "unit_kind": "page", "content": text},
                ]
            )
        )
        source = catalog.admit(body, source_key=name)
        processing = catalog.retain_processing(
            source.revision_id,
            {"body.md": body, "native.json": native},
            body_path="body.md",
            source_path="native.json",
            source_kind=kind,
            fingerprint="policy",
            operation_id=name,
            decision=classify_markdown_tokens(2),
        )
        units.append(catalog.plan_unit(processing.processing_id, key=name, doc_name=name))
    knowledge = KnowledgeStore(root, identity)
    old = knowledge.publish(
        knowledge.prepare_revision({"index.md": b"Still readable"}, expected=None)
    )
    with pytest.raises(ValueError, match="aliases"):
        knowledge.prepare_revision(
            {"index.md": b"Unusable aliases"},
            expected=old.publication_id,
            unit_revisions=tuple(unit.unit_revision_id for unit in units),
        )
    assert knowledge.bind().read("index.md") == b"Still readable"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocks", [False, True])
async def test_retained_native_actions_execute_without_colliding_with_a_document_name(
    tmp_path, blocks
):
    import hashlib
    import json

    from agents import Agent
    from agents.tool_context import ToolContext

    from ultrakb.agent.query_evidence import restrict_query_agent
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.views import storage_scope
    from ultrakb.content_blocks import BLOCK_POLICY, split_blocks
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.source_reading import source_capabilities
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.text_source import FrozenText, TextOrigin

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog, knowledge = CatalogStore(root, identity), KnowledgeStore(root, identity)
    body, native = tmp_path / "body.md", tmp_path / "native.json"
    expected, old_source = None, None
    for name, text in (
        ("original", "OLD_FROZEN_TEXT"),
        ("original (revision 1)", "NEW_FROZEN_TEXT"),
    ):
        body.write_text(text)
        native.write_text(
            FrozenText(
                text=text,
                original_characters=len(text),
                original_digest=hashlib.sha256(text.encode()).hexdigest(),
                origins=(TextOrigin(normalized_span=(0, len(text)), original_span=(0, len(text))),),
                assets={},
                tokens=0,
                unit_kind="block" if blocks else "text",
                blocks=split_blocks(text) if blocks else (),
                block_policy=BLOCK_POLICY if blocks else None,
            ).model_dump_json()
        )
        source = catalog.admit(body, source_key=name)
        old_source = old_source or source
        processing = catalog.retain_processing(
            source.revision_id,
            {"body.md": body, "native.json": native},
            body_path="body.md",
            source_path="native.json",
            source_kind="text",
            fingerprint="policy",
            operation_id=name,
            decision=classify_markdown_tokens(2),
        )
        unit = catalog.plan_unit(processing.processing_id, key="body", doc_name=name)
        publication = knowledge.publish(
            knowledge.prepare_revision(
                {"concepts/old.md": b"---\nsources: [original]\n---\nOld conclusion"},
                expected=expected,
                unit_revisions=(unit.unit_revision_id,),
            )
        )
        expected = publication.publication_id
    selected = resolve_query_views(root, scope=storage_scope(root, identity))
    sources = source_capabilities(selected.views[0])
    assert len(sources) == 2
    retained = next(
        source
        for source in sources.values()
        if source["source_revision_id"] == old_source.revision_id
    )
    action = retained["reading_action"]
    assert action["arguments"]["doc_name"] != "original (revision 1)"
    agent = Agent(name="Bound reader", instructions="Read the supplied native source.")
    agent = restrict_query_agent(agent, selected)
    tool = next(tool for tool in agent.tools if tool.name == action["tool"])
    payload = json.dumps(action["arguments"])
    result = await tool.on_invoke_tool(
        ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id="retained-source",
            tool_arguments=payload,
        ),
        payload,
    )
    assert "OLD_FROZEN_TEXT" in result
    assert "NEW_FROZEN_TEXT" not in result


@pytest.mark.parametrize("declaration", ["sources/foo_bar.md", "sources/FOO_BAR.md"])
def test_page_source_names_are_exact_before_using_an_unambiguous_alias(tmp_path, declaration):
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.reading import read_page_context
    from ultrakb.application.views import storage_scope
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.source_context import reading_metadata
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    identity = create_database(root)
    catalog = CatalogStore(root, identity)
    original = tmp_path / "body.md"
    units, sources = [], []
    for name in ("foo_bar", "foo-bar"):
        original.write_text(name)
        source = catalog.admit(original, source_key=name)
        sources.append(source)
        processing = catalog.retain_processing(
            source.revision_id,
            {"body.md": original},
            body_path="body.md",
            fingerprint="policy",
            operation_id=name,
            decision=classify_markdown_tokens(2),
        )
        units.append(catalog.plan_unit(processing.processing_id, key="body", doc_name=name))
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_revision(
            {"concepts/one.md": f"---\nsources: [{declaration}]\n---\nConclusion".encode()},
            expected=None,
            unit_revisions=tuple(unit.unit_revision_id for unit in units),
        )
    )
    scope = storage_scope(root, identity)
    context = read_page_context(root, "concepts/one", scope=scope)
    view = resolve_query_views(root, scope=scope).views[0]
    evidence = reading_metadata(view, "concepts/one.md")["original_sources"]
    if declaration == "sources/foo_bar.md":
        assert [link.path for link in context.sources] == ["sources/foo_bar"]
        assert [source["source_revision_id"] for source in evidence] == [sources[0].revision_id]
    else:
        assert context.sources[0].path is None
        assert evidence == []
