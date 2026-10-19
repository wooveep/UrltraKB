"""Compiler diagnostics survive publication and bounded recovery without another send."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_repaired_plan_quality_is_retained_with_the_exact_committed_result(tmp_path):
    root, identity, target = compile_input(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"unexpected": True})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert first.status == "compiled", first.message
        assert "compile_plan_repaired" in first.quality
        assert first.quality_known is True
        assert first.units[0].quality == first.quality
        recovered = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert recovered.status == "compiled", recovered.message
        assert recovered.quality == first.quality
        assert recovered.quality_known is True
        assert recovered.version == first.version
        assert len(service.requests) == 3


@pytest.mark.asyncio
async def test_publication_failure_retains_accepted_repair_facts_when_a_later_run_succeeds(
    tmp_path,
):
    from ultrakb.storage.coordination import storage_stage

    root, identity, target = compile_input(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_diagnostic_publication BEFORE INSERT ON ukb_publications "
            "BEGIN SELECT RAISE(ABORT, 'Publication interrupted'); END"
        )
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"unexpected": True})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        failed = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert failed.status == "blocked" and "compile_plan_repaired" in failed.quality
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER fail_diagnostic_publication")
        result = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert result.status == "compiled", result.message
        assert "compile_plan_repaired" in result.quality and not result.unfinished
        assert len(service.requests) == 3


@pytest.mark.asyncio
async def test_execution_cleanup_failure_keeps_the_determined_compiler_failure(
    tmp_path, monkeypatch
):
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity, target = compile_input(tmp_path)
    original = ExecutionStore.finish

    def unacknowledged(*args, **options):
        original(*args, **options)
        raise OSError("Execution finish acknowledgement lost")

    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"unexpected": True})},
            {"content": json.dumps({"unexpected": True})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:
            failure.setattr(ExecutionStore, "finish", unacknowledged)
            result = await recompile_document(
                root, target.source_id, unit_id=target.unit_id, scope=storage_scope(root, identity)
            )
        assert result.status == "failed", result.message
        assert "malformed_plan_items" in result.quality and result.quality_known is True
        assert {"concepts", "entities", "confirm execution cleanup"} <= set(result.unfinished)
        assert len(service.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True], ids=["normal", "retained-partition"])
async def test_large_catalog_planning_uses_retained_partitions_and_keeps_the_whole_catalog(
    tmp_path, monkeypatch, interrupt
):
    from ultrakb.config import save_config
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, target = compile_input(tmp_path)
    store = KnowledgeStore(root, identity)
    pages = {
        f"concepts/topic-{n}.md": (
            "---\ndescription: "
            + (f"Distinct topic {n} with full conditions. " * 30)
            + "\n---\n# Topic\n"
        ).encode()
        for n in range(48)
    }
    store.publish(store.prepare_changes(store.bind(), pages))
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 8, "retries": 0},
            "model_capacity": {"max_input_bytes": 24000},
        },
    )
    partitions = []

    def respond(request, index):
        message = next(
            (
                item["content"]
                for item in request["messages"]
                if isinstance(item.get("content"), str)
                and item["content"].startswith("Planning partition (")
            ),
            None,
        )
        if message:
            value = json.loads(message.split("\n", 1)[1])
            partitions.extend(item["node_id"] for item in value["work"])
            return {
                "content": json.dumps(
                    {
                        "coverage": [
                            {
                                "id": item["node_id"],
                                "candidates": [],
                                "reason": "No independent page needed.",
                            }
                            for item in value["work"]
                        ],
                        "candidates": [],
                    }
                )
            }
        return {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})}

    context = ExecutionContext()
    scope = storage_scope(root, identity)
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        if interrupt:
            from ultrakb.storage.checkpoint_store import CompileWork

            original = CompileWork.save

            def interrupted(self, key, *args, **options):
                result = original(self, key, *args, **options)
                if key.startswith("partition:candidates:"):
                    raise OSError("Partition acknowledgement interrupted")
                return result

            with monkeypatch.context() as failure:
                failure.setattr(CompileWork, "save", interrupted)
                stopped = await recompile_document(
                    root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
                )
                assert stopped.status == "blocked"
                assert len(service.requests) == 2
        result = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert result.status == "compiled", (result.message, result.quality)
        assert "compile_planning_partitioned" in result.quality
        assert partitions and len(service.requests) == 2
        resumed = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert resumed.status == "compiled" and resumed.quality == result.quality
        assert len(service.requests) == 2
    assert all(store.bind().read(path) == content for path, content in pages.items())


@pytest.mark.asyncio
async def test_exhausted_plan_failure_reports_the_retained_stage_and_never_resets_repair(tmp_path):
    root, identity, target = compile_input(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"unexpected": True})},
            {"content": json.dumps({"unexpected": True})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        failed = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert failed.status == "failed", failed.message
        assert "malformed_plan_items" in failed.quality
        assert {"concepts", "entities"} <= set(failed.unfinished)
        assert storage_scope(root, identity).publication_id == scope.publication_id
        resumed = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert resumed.status == "failed", resumed.message
        assert "malformed_plan_items" in resumed.quality
        assert len(service.requests) == 3


@pytest.mark.asyncio
async def test_identity_reasoning_compaction_resumes_accepted_work_without_repeating_requests(
    tmp_path, monkeypatch
):
    from ultrakb.config import save_config
    from ultrakb.storage.checkpoint_store import CompileWork
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, target = compile_input(tmp_path)
    store = KnowledgeStore(root, identity)
    pages = {
        f"concepts/topic-{n}.md": (
            "---\ndescription: "
            + (f"Distinct mechanism {n}. " * (5000 if n == 0 else 30))
            + "\n---\n# Topic\n"
        ).encode()
        for n in range(8)
    }
    store.publish(store.prepare_changes(store.bind(), pages))
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 128, "retries": 0},
            "model_capacity": {"max_input_bytes": 24000},
        },
    )
    compacted, compared = [], set()

    def respond(request, index):
        texts = [
            message["content"]
            for message in request["messages"]
            if isinstance(message.get("content"), str)
        ]
        for message in texts:
            if message.startswith("Planning partition ("):
                value = json.loads(message.split("\n", 1)[1])
                work = value["work"][0]
                return {
                    "content": json.dumps(
                        {
                            "coverage": [
                                {
                                    "id": item["node_id"],
                                    "candidates": ["proposal"],
                                    "reason": "Existing topic contribution.",
                                }
                                for item in value["work"]
                            ],
                            "candidates": [
                                {
                                    "id": "proposal",
                                    "kind": "concepts",
                                    "name": "candidate",
                                    "title": "Candidate",
                                    "brief": "A source contribution.",
                                    "reading_locations": work["reading_locations"],
                                }
                            ],
                        }
                    )
                }
            if message.startswith("Candidate identity comparison:"):
                value = json.loads(message.split("\n", 1)[1])
                compared.update(entry["id"] for entry in value["catalog"])
                return {
                    "content": json.dumps(
                        {
                            "matches": [
                                {
                                    "id": entry["id"],
                                    "relation": "same"
                                    if entry["id"] == "concepts/topic-7"
                                    else "different",
                                    "action": "related",
                                    "reason": "Distinct scope. " * 3000
                                    if entry["id"] == "concepts/topic-0"
                                    and "fragment_decisions" not in entry
                                    else "Confirmed distinct scope or canonical topic.",
                                }
                                for entry in value["catalog"]
                            ]
                        }
                    )
                }
            if message.startswith("Compact identity reasoning"):
                value = json.loads(message.split("\n", 1)[1])
                compacted.append(value["max_characters"])
                return {"content": json.dumps({"reason": "Distinct"[: value["max_characters"]]})}
        return {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})}

    original = CompileWork.save

    def interrupted(self, key, *args, **options):
        value = original(self, key, *args, **options)
        if key.startswith("partition:identity-reason:"):
            raise OSError("Compacted identity acknowledgement interrupted")
        return value

    context, scope = ExecutionContext(), storage_scope(root, identity)
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:
            failure.setattr(CompileWork, "save", interrupted)
            failed = await recompile_document(
                root, target.source_id, unit_id=target.unit_id, context=context, scope=scope
            )
        assert failed.status == "blocked", (failed.message, len(service.requests))
        assert compacted
        sent = list(service.requests)
        result = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, context=context, scope=scope
        )
        assert result.status == "compiled", (result.message, result.quality, len(service.requests))
        # Already accepted summary, candidate, fragment decisions and first
        # compaction are read back; subsequent sends continue different work.
        assert all(request not in sent for request in service.requests[len(sent) :])
        assert len(service.requests) < 128
    assert compared == {f"concepts/topic-{n}" for n in range(8)}
    assert "concepts/candidate.md" not in store.bind().files()
    assert store.bind().read("concepts/topic-0.md") == pages["concepts/topic-0.md"]
