"""Actual compiler calls retain their inputs and results in the main store."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.pages import read_page
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore

pytestmark = pytest.mark.integration


def compile_input(tmp_path, *, segmented=False, body_text="The value is 17."):
    root, identity, _ = question_knowledge(tmp_path)
    catalog = CatalogStore(root, identity)
    original = tmp_path / "fixture.txt"
    original.write_text(body_text)
    source = catalog.admit(original, source_key="fixture")
    body = tmp_path / "fixture.md"
    body.write_text(body_text)
    processing = catalog.retain_processing(
        source.revision_id,
        {"fixture.md": body},
        body_path="fixture.md",
        fingerprint="text-v1",
        operation_id="fixture-processing",
        decision=classify_markdown_tokens(5001 if segmented else 5),
    )
    unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="fixture")
    original.unlink()
    body.unlink()
    return root, identity, unit


@pytest.mark.asyncio
async def test_recompile_uses_retained_input_and_publishes_actual_compiler_output(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    with StreamTransport(
        [
            {"content": summary},
            {"content": json.dumps({"concepts": [], "entities": []})},
            {"content": summary},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=storage_scope(root, identity)
        )
    assert result.status == "compiled", result.message
    assert not result.unfinished
    assert "17" in read_page(root, "summaries/fixture.md", scope=storage_scope(root, identity)).body
    assert service.requests
    for name in (
        "hashes.json",
        "catalog",
        "compile-checkpoints",
        "compile-responses",
        "model-tasks",
    ):
        assert not (root / ".ultrakb" / name).exists()


@pytest.mark.asyncio
async def test_compile_record_encoding_does_not_hold_the_sql_writer(tmp_path, monkeypatch):
    from ultrakb.storage import checkpoint_store, compile_deliveries
    from ultrakb.storage.database import open_database

    root, identity, unit = compile_input(tmp_path)
    encode, observed = checkpoint_store._json, []

    def encoded(value, **options):
        if isinstance(value, dict) and ("latest_invocation" in value or "latest_response" in value):
            with open_database(root, expected=identity) as probe:
                probe.connection.execute("PRAGMA busy_timeout = 0")
                probe.connection.execute("BEGIN IMMEDIATE")
                probe.connection.execute("ROLLBACK")
            observed.append(True)
        return encode(value, **options)

    monkeypatch.setattr(checkpoint_store, "_json", encoded)
    monkeypatch.setattr(compile_deliveries, "_json", encoded)
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    with StreamTransport(
        [{"content": summary}, {"content": json.dumps({"concepts": [], "entities": []})}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            scope=storage_scope(root, identity),
        )
    assert result.status == "compiled", result.message
    assert observed


@pytest.mark.asyncio
async def test_received_summary_survives_failure_before_accepted_checkpoint(tmp_path, monkeypatch):
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity, unit = compile_input(tmp_path)
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    original_save = CompileWork.save

    def interrupted_save(self, key, *args, **kwargs):
        if key == "short-summary":
            raise OSError("checkpoint acknowledgement interrupted")
        return original_save(self, key, *args, **kwargs)

    context = ExecutionContext()
    with StreamTransport(
        [
            {"content": summary},
            {"content": json.dumps({"concepts": [], "entities": []})},
            {"content": summary},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:
            failure.setattr(CompileWork, "save", interrupted_save)
            failed = await recompile_document(
                root,
                unit.source_id,
                unit_id=unit.unit_id,
                context=context,
                scope=storage_scope(root, identity),
            )
        assert failed.status == "blocked"
        assert len(service.requests) == 1
        recovered = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert recovered.status == "compiled", recovered.message
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_received_plan_survives_compiler_delivery_failure_without_resend(
    tmp_path, monkeypatch
):
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity, unit = compile_input(tmp_path)
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    record_received = CompileWork.record_received_response

    def interrupted(self, key, *args, **kwargs):
        if key == "plan":
            raise OSError("plan delivery interrupted")
        return record_received(self, key, *args, **kwargs)

    context = ExecutionContext()
    with StreamTransport(
        [{"content": summary}, {"content": json.dumps({"concepts": [], "entities": []})}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:
            failure.setattr(CompileWork, "record_received_response", interrupted)
            failed = await recompile_document(
                root,
                unit.source_id,
                unit_id=unit.unit_id,
                context=context,
                scope=storage_scope(root, identity),
            )
        assert failed.status == "blocked"
        assert len(service.requests) == 2
        recovered = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert recovered.status == "compiled", recovered.message
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_invalid_summary_uses_one_repair_and_preserves_paid_attempts(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    with StreamTransport(
        [
            {"content": "invalid summary"},
            {"content": summary},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            scope=storage_scope(root, identity),
        )
        assert result.status == "compiled", result.message
        assert len(service.requests) == 3


@pytest.mark.asyncio
async def test_new_paid_plan_repair_is_recovered_before_older_invalid_delivery(
    tmp_path, monkeypatch
):
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity, unit = compile_input(tmp_path)
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    received = CompileWork.record_received_response

    def interrupted(self, key, *args, **kwargs):
        if key == "plan" and kwargs.get("attempt") == 1:
            raise OSError("repaired plan delivery interrupted")
        return received(self, key, *args, **kwargs)

    context = ExecutionContext()
    with StreamTransport(
        [
            {"content": summary},
            {"content": "invalid plan"},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:
            failure.setattr(CompileWork, "record_received_response", interrupted)
            failed = await recompile_document(
                root,
                unit.source_id,
                unit_id=unit.unit_id,
                context=context,
                scope=storage_scope(root, identity),
            )
        assert failed.status == "blocked"
        assert len(service.requests) == 3
        recovered = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert recovered.status == "compiled", recovered.message
        assert len(service.requests) == 3


@pytest.mark.asyncio
async def test_compiler_creates_page_with_complete_published_markdown_visibility(tmp_path):
    from ultrakb.config import save_config
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, unit = compile_input(tmp_path)
    knowledge = KnowledgeStore(root, identity)
    keep = b"---\ndescription: Retained context\naliases: [Preserved]\n---\n# Keep\n\nKnown fact.\n"
    knowledge.publish(knowledge.prepare_changes(knowledge.bind(), {"concepts/keep.md": keep}))
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model_policy": {"max_calls": 8, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    (root / "wiki/concepts").mkdir(parents=True, exist_ok=True)
    (root / "wiki/concepts/keep.md").write_text("UNACCEPTED LIVE DRAFT")
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    plan = json.dumps(
        {
            "concepts": {
                "create": [{"name": "recorded-value", "title": "Recorded value"}],
                "update": [],
                "related": [],
            },
            "entities": [],
        }
    )
    page = json.dumps(
        {"description": "Recorded value", "content": "# Recorded value\n\nThe value is 17."}
    )
    with StreamTransport(
        [{"content": summary}, {"content": plan}, {"content": page}, {"content": summary}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            scope=storage_scope(root, identity),
        )
        assert result.status == "compiled", result.message
        sent = json.dumps(service.requests)
        assert "UNACCEPTED LIVE DRAFT" not in sent
        assert "keep" in sent
    scope = storage_scope(root, identity)
    assert "17" in read_page(root, "concepts/recorded-value.md", scope=scope).body
    assert knowledge.bind().read("concepts/keep.md") == keep


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True], ids=["small-metadata", "large-metadata"])
async def test_long_recompile_uses_shared_ready_tree_and_structured_original_summary(
    tmp_path, large
):
    from ultrakb.config import save_config
    from ultrakb.storage.index_store import IndexStore
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, unit = compile_input(
        tmp_path,
        segmented=True,
        body_text="The value is 17. " + ("Retained additional input. " * 4000 if large else ""),
    )
    if large:
        save_config(
            root / ".ultrakb/config.yaml",
            {
                "model_policy": {"max_calls": 10, "retries": 0},
                "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            },
        )
    indexes = IndexStore(root, identity)
    accepted = indexes.accept(
        unit.unit_revision_id,
        {
            "doc_id": "fixture-document",
            "doc_name": "fixture",
            "structure": [
                {
                    "node_id": "fixture-body",
                    "title": "Fixture",
                    "text": "The value is 17.",
                    "start_index": 1,
                    "end_index": 1,
                    "source_locator": {"kind": "text", "start_char": 0, "end_char": 16},
                }
            ],
        },
        content_fingerprint="a" * 64,
    )
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_revision(
            {"index.md": b"Fixture"},
            expected=knowledge.bind().publication_id,
            index_revisions=(accepted.index_revision_id,),
        )
    )

    def response(request, index):
        original = next(
            (
                m["content"]
                for m in request["messages"]
                if isinstance(m.get("content"), str)
                and m["content"].startswith("Original document: ")
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
                                "parts": [
                                    {"kind": "prose", "text": "The value is 17.", "refs": []}
                                ],
                            }
                            for section in sections
                        ],
                    }
                )
            }
        return {"content": json.dumps({"concepts": [], "entities": []})}

    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            scope=storage_scope(root, identity),
        )
        assert result.status == "compiled", result.message
        assert len(service.requests) >= 2
    assert "17" in read_page(root, "summaries/fixture.md", scope=storage_scope(root, identity)).body
    assert knowledge.bind().index_revisions() == (accepted.index_revision_id,)
    assert indexes.document(accepted.index_revision_id)["doc_id"] == "fixture-document"
    assert not list((root / ".ultrakb").glob("**/context.sqlite"))


@pytest.mark.asyncio
async def test_publish_failure_resumes_accepted_compiler_steps_without_sending(
    tmp_path, monkeypatch
):
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, unit = compile_input(tmp_path)
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    context = ExecutionContext()
    with StreamTransport(
        [{"content": summary}, {"content": json.dumps({"concepts": [], "entities": []})}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:

            def fail_publish(*args, **kwargs):
                raise OSError("publication interrupted")

            failure.setattr(KnowledgeStore, "publish", fail_publish)
            failed = await recompile_document(
                root,
                unit.source_id,
                unit_id=unit.unit_id,
                context=context,
                scope=storage_scope(root, identity),
            )
        assert failed.status == "blocked"
        assert len(service.requests) == 2
        recovered = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert recovered.status == "compiled", recovered.message
        assert len(service.requests) == 2
    assert "17" in read_page(root, "summaries/fixture.md", scope=storage_scope(root, identity)).body


@pytest.mark.asyncio
async def test_published_compilation_survives_projection_coordination_failure(
    tmp_path, monkeypatch
):
    from ultrakb.mutation import RecoveryRequired
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, unit = compile_input(tmp_path)
    context = ExecutionContext()
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})

    def unavailable(*args, **kwargs):
        raise RecoveryRequired("projection lock receipt unavailable")

    monkeypatch.setattr(ProjectionStore, "sync", unavailable)
    with StreamTransport(
        [{"content": summary}, {"content": json.dumps({"concepts": [], "entities": []})}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root,
            unit.source_id,
            unit_id=unit.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
    assert result.status == "compiled", result.message
    assert result.version == storage_scope(root, identity).publication_id
    assert result.unfinished
    from ultrakb.application.knowledge_tasks import read_knowledge_execution

    detail = read_knowledge_execution(root, context.storage_execution.execution_id)
    assert detail.execution.state == "completed"
    assert detail.publications[0].publication_id == result.version
    assert "17" in read_page(root, "summaries/fixture.md", scope=storage_scope(root, identity)).body


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True], ids=["normal", "delivery-interrupted"])
async def test_shared_page_update_keeps_old_original_distinct_from_new_target(
    tmp_path, monkeypatch, interrupt
):
    from ultrakb.config import save_config
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, old = compile_input(tmp_path)
    knowledge = KnowledgeStore(root, identity)
    previous = knowledge.publish(
        knowledge.prepare_revision(
            {
                "index.md": b"[[concepts/value]]",
                "summaries/fixture.md": (
                    b"---\nfull_text: sources/fixture.md\n---\nThe value is 17.\n"
                ),
                "concepts/value.md": (
                    b"---\ndescription: Recorded value\nsources: [summaries/fixture.md]\n"
                    b"---\n# Value\n\nThe value is 17.\n"
                ),
            },
            expected=knowledge.bind().publication_id,
            unit_revisions=(old.unit_revision_id,),
        )
    )
    catalog = CatalogStore(root, identity)
    changed = tmp_path / "fixture.txt"
    changed.write_text("The value is 23.")
    source = catalog.admit(changed, source_key="fixture", expected=old.source_revision_id)
    processing = catalog.retain_processing(
        source.revision_id,
        {"fixture.md": changed},
        body_path="fixture.md",
        fingerprint="text-v1",
        operation_id="changed-processing",
        decision=classify_markdown_tokens(5),
    )
    target = catalog.plan_unit(
        processing.processing_id, key="body", doc_name="fixture", expected_generation=old.generation
    )
    changed.unlink()
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model_policy": {"max_calls": 10, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    summary = json.dumps({"description": "Fixture", "content": "The value is 23."})
    plan = json.dumps(
        {
            "concepts": {
                "create": [],
                "update": [{"name": "value", "title": "Value"}],
                "related": [],
            },
            "entities": [],
        }
    )

    updates = []

    def response(request, index):
        update = next(
            (
                m["content"]
                for m in request["messages"]
                if isinstance(m.get("content"), str)
                and m["content"].startswith("Temporary editable page blocks:\n")
            ),
            None,
        )
        if update:
            updates.append(update)
            captured, _ = json.JSONDecoder().raw_decode(update.split("\n", 1)[1])
            block = next(b["id"] for b in captured["blocks"] if "The value is 17." in b["text"])
            return {
                "content": json.dumps(
                    {
                        "status": "updated",
                        "base_sha256": captured["base_sha256"],
                        "edits": [
                            {
                                "block_id": block,
                                "operation": "replace",
                                "content": "The value is 23.\n",
                            }
                        ],
                    }
                )
            }
        return {"content": plan if index == 1 else summary}

    context = ExecutionContext()
    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        if interrupt:
            from ultrakb.storage.checkpoint_store import CompileWork

            received = CompileWork.record_received_response

            def interrupted(self, key, *args, **kwargs):
                if key.startswith("page:"):
                    raise OSError("shared page delivery interrupted")
                return received(self, key, *args, **kwargs)

            with monkeypatch.context() as failure:
                failure.setattr(CompileWork, "record_received_response", interrupted)
                failed = await recompile_document(
                    root,
                    target.source_id,
                    unit_id=target.unit_id,
                    context=context,
                    scope=storage_scope(root, identity),
                )
            assert failed.status == "failed", (failed.message, failed.quality, failed.unfinished)
            assert "concept_generation_incomplete" in failed.quality
        result = await recompile_document(
            root,
            target.source_id,
            unit_id=target.unit_id,
            context=context,
            scope=storage_scope(root, identity),
        )
        assert result.status == "compiled", result.message
        sent = json.dumps(service.requests)
        assert f"sources/retained/{old.unit_revision_id}/fixture.md" in sent
        assert "The value is 17." in sent and "The value is 23." in sent
        assert len(updates) == 1
    assert "23" in read_page(root, "concepts/value.md", scope=storage_scope(root, identity)).body
    assert "17" in knowledge.bind(previous.publication_id).read("concepts/value.md").decode()
