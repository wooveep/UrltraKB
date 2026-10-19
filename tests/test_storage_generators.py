"""Generators retain artifacts independently of mutable output projections."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.generators import GenerationOptions, generate_artifact, preview_generation
from ultrakb.application.views import storage_scope
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def generator_knowledge(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    store = KnowledgeStore(root, identity)
    store.publish(
        store.prepare_revision(
            {
                "index.md": b"# Index\n\n[[concepts/value]]\n",
                "concepts/value.md": b"# Value\n\nThe recorded value is 17.\n",
            },
            expected=store.bind().publication_id,
            unit_revisions=(unit.unit_revision_id,),
            page_inputs={"concepts/value.md": (unit.unit_revision_id,)},
        )
    )
    return root, identity


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_skill_generation_reads_fixed_knowledge_and_retains_its_output_in_the_main_store(
    tmp_path, concurrent
):
    root, identity = generator_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    preview = preview_generation(root, "skill", "explain-value", scope=scope)
    skill = (
        "---\nname: explain-value\ndescription: Use to explain the recorded value.\n"
        "---\n# Value\n\nThe recorded value is 17.\n"
    )
    responses = [
        {
            "tool_calls": [
                {
                    "id": "read-value",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"concepts/value.md"}'},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "id": "write-skill",
                    "type": "function",
                    "function": {
                        "name": "write_skill_file",
                        "arguments": json.dumps({"path": "SKILL.md", "content": skill}),
                    },
                }
            ]
        },
        {"content": "Skill delivered."},
    ]

    def respond(request, index):
        if concurrent and index == 2:
            from ultrakb.application.execution import ExecutionContext
            from ultrakb.application.storage_execution import begin_storage_operation

            other = ExecutionContext(storage_identity=identity)
            with begin_storage_operation(root, other, "concurrent-artifact"):
                store = ArtifactStore(root, identity)
                store.save(
                    "output/skills/explain-value/SKILL.md",
                    b"A concurrently accepted skill.",
                    expected_epoch=store.epoch(),
                    execution_id=other.storage_execution.execution_id,
                    evidence_publication_id=scope.publication_id,
                )
                other._storage_result = "completed"
        return responses[index]

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await generate_artifact(
            root,
            GenerationOptions(
                "skill", "explain-value", "Explain the recorded value.", version=preview.version
            ),
            scope=scope,
        )
    assert result.status == ("conflict" if concurrent else "completed"), result
    assert ArtifactStore(root, identity).read("output/skills/explain-value/SKILL.md") == (
        b"A concurrently accepted skill." if concurrent else skill.encode()
    )
    assert storage_scope(root, identity).publication_id == scope.publication_id
    assert any(
        message["role"] == "tool" and "The recorded value is 17." in message["content"]
        for message in service.requests[1]["messages"]
    )
    assert len(service.requests) == 3
    if not concurrent:
        manifest = json.loads(ArtifactStore(root, identity).read(".claude-plugin/marketplace.json"))
        assert manifest["name"] == "vectify"
        assert manifest["plugins"][0]["skills"] == ["./output/skills/explain-value"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption", ["before-save", "after-save", "projection", "second-save", "plan-ack"]
)
async def test_skill_output_recovery_keeps_the_paid_generation_and_exact_acceptance(
    tmp_path, monkeypatch, interruption
):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.storage.generation_store import GenerationStore

    root, identity = generator_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    preview = preview_generation(root, "skill", "explain-value", scope=scope)
    options = GenerationOptions("skill", "explain-value", "Explain value.", version=preview.version)
    skill = (
        "---\nname: explain-value\ndescription: Explain the recorded value.\n---\n# Value\n\n17\n"
    )
    responses = [
        {
            "tool_calls": [
                {
                    "id": "write-skill",
                    "type": "function",
                    "function": {
                        "name": "write_skill_file",
                        "arguments": json.dumps({"path": "SKILL.md", "content": skill}),
                    },
                }
            ]
        },
        {"content": "Complete."},
    ]
    save = ArtifactStore.save
    prepare = GenerationStore.prepare

    def prepared_before_interrupt(self, *args, **kwargs):
        prepare(self, *args, **kwargs)
        raise OSError("Generator plan acknowledgement interrupted")

    def interrupted(self, *args, **kwargs):
        if interruption == "second-save" and kwargs["path"].startswith("output/"):
            return save(self, *args, **kwargs)
        if interruption == "after-save":
            save(self, *args, **kwargs)
        raise OSError("Artifact I/O interrupted")

    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            if interruption == "plan-ack":
                fault.setattr(GenerationStore, "prepare", prepared_before_interrupt)
            else:
                fault.setattr(
                    ArtifactStore, "sync" if interruption == "projection" else "save", interrupted
                )
            first = await generate_artifact(root, options, scope=scope, context=context)
        assert first.status == (
            "failed" if interruption in {"before-save", "second-save", "plan-ack"} else "completed"
        ), first
        if interruption == "second-save":
            assert str(root / "output/skills/explain-value/SKILL.md") in first.resources
        if interruption == "projection":
            assert first.projection_pending
        result = await generate_artifact(root, options, scope=scope, context=context)
        assert result.status == "completed" and not result.projection_pending, result
        assert ArtifactStore(root, identity).epoch() == 2
        assert (
            ArtifactStore(root, identity).read("output/skills/explain-value/SKILL.md")
            == skill.encode()
        )
        assert storage_scope(root, identity).publication_id == scope.publication_id
        assert len(service.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_request", ["agent", "rank"])
async def test_model_failure_keeps_the_skill_file_already_delivered_by_its_tool(
    tmp_path, failed_request
):
    from ultrakb.application.execution import ExecutionContext

    root, identity = generator_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    preview = preview_generation(root, "skill", "explain-value", scope=scope)
    options = GenerationOptions("skill", "explain-value", "Explain value.", version=preview.version)
    skill = (
        "---\nname: explain-value\ndescription: Explain the recorded value.\n---\n# Value\n\n17\n"
    )
    responses = [
        {
            "tool_calls": [
                {
                    "id": "write-skill",
                    "type": "function",
                    "function": {
                        "name": "write_skill_file",
                        "arguments": json.dumps({"path": "SKILL.md", "content": skill}),
                    },
                }
            ]
        },
        {"error": {"message": "Generation could not finish.", "type": "offline_error"}},
    ]
    if failed_request == "rank":
        responses.insert(
            1,
            {
                "tool_calls": [
                    {
                        "id": "search-value",
                        "type": "function",
                        "function": {
                            "name": "search_knowledge",
                            "arguments": '{"query":"recorded value"}',
                        },
                    }
                ]
            },
        )
    with StreamTransport(responses, statuses=[*[200] * (len(responses) - 1), 400]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await generate_artifact(root, options, scope=scope, context=context)
        assert result.status == "failed" and result.error_type == (
            "BadRequestError" if failed_request == "agent" else "UserError"
        ), result
        assert str(root / "output/skills/explain-value/SKILL.md") in result.resources
        assert result.validation is None and "generation" in result.unfinished
        assert (
            ArtifactStore(root, identity).read("output/skills/explain-value/SKILL.md")
            == skill.encode()
        )
        replay = await generate_artifact(root, options, scope=scope, context=context)
        assert replay.status == "failed" and replay.resources == result.resources
        assert len(service.requests) == len(responses)


@pytest.mark.asyncio
async def test_known_artifact_acceptance_survives_a_persistent_database_read_failure(
    tmp_path, monkeypatch
):
    from ultrakb.application.execution import ExecutionContext

    root, identity = generator_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    options = GenerationOptions("skill", "explain-value", "Explain the recorded value.")
    skill = "---\nname: explain-value\ndescription: Explain the value.\n---\n# Value\n17\n"
    save, moved = ArtifactStore.save, []

    def fail_after_first_commit(self, *args, **kwargs):
        revision = save(self, *args, **kwargs)
        if kwargs.get("path", "").endswith("/SKILL.md"):
            shm = next(root.rglob("kb.sqlite-shm"))
            unavailable = shm.with_suffix(".unavailable")
            shm.rename(unavailable)
            moved.append((shm, unavailable))
        return revision

    responses = [
        {
            "tool_calls": [
                {
                    "id": "write-skill",
                    "type": "function",
                    "function": {
                        "name": "write_skill_file",
                        "arguments": json.dumps({"path": "SKILL.md", "content": skill}),
                    },
                }
            ]
        },
        {"content": "Skill delivered."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        try:
            with monkeypatch.context() as fault:
                fault.setattr(ArtifactStore, "save", fail_after_first_commit)
                result = await generate_artifact(root, options, scope=scope, context=context)
        finally:
            for shm, unavailable in moved:
                unavailable.rename(shm)
        assert result.status == "failed" and result.generation_id is not None, result
        assert result.artifact_epoch == 1 and result.projection_pending
        assert str(root / "output/skills/explain-value/SKILL.md") in result.resources
        assert (
            ArtifactStore(root, identity).read("output/skills/explain-value/SKILL.md")
            == skill.encode()
        )
        assert len(service.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "search,stop_at",
    [
        (False, "agent"),
        (True, "agent"),
        (True, "reader"),
        (True, "rank"),
        (True, "reader-io"),
        (True, "rank-io"),
    ],
)
async def test_interrupted_generation_reuses_its_selection_and_paid_tool_results(
    tmp_path, monkeypatch, search, stop_at
):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.storage_execution import begin_storage_operation
    from ultrakb.locks import LockCancelled
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity = generator_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    options = GenerationOptions("skill", "explain-value", "Explain the recorded value.")
    skill = "---\nname: explain-value\ndescription: Explain the value.\n---\n# Value\n17\n"
    kinds = []

    def respond(request, index):
        definitions = [item["function"] for item in request.get("tools", [])]
        rank = next((item for item in definitions if item["name"] == "rank"), None)
        if rank:
            kinds.append("rank")
            aliases = rank["parameters"]["properties"]["ranked_ids"]["items"]["enum"]
            return {
                "tool_calls": [
                    {
                        "id": "rank",
                        "type": "function",
                        "function": {
                            "name": "rank",
                            "arguments": json.dumps({"ranked_ids": aliases, "done": False}),
                        },
                    }
                ]
            }
        kinds.append("agent")
        tools = [message for message in request["messages"] if message["role"] == "tool"]
        if search and not tools:
            return {
                "tool_calls": [
                    {
                        "id": "search-value",
                        "type": "function",
                        "function": {
                            "name": "search_knowledge",
                            "arguments": '{"query":"recorded value"}',
                        },
                    }
                ]
            }
        if not any(item.get("tool_call_id") == "write-skill" for item in tools):
            return {
                "tool_calls": [
                    {
                        "id": "write-skill",
                        "type": "function",
                        "function": {
                            "name": "write_skill_file",
                            "arguments": json.dumps({"path": "SKILL.md", "content": skill}),
                        },
                    }
                ]
            }
        return {"content": "Skill delivered."}

    save = CompileWork.save

    def interrupted(self, key, *args, **kwargs):
        if (
            (stop_at == "agent" and key == "maintenance-agent:1")
            or (
                stop_at.startswith("reader")
                and key.startswith("maintenance-tool:")
                and ":model:" not in key
            )
            or (stop_at.startswith("rank") and ":model:" in key)
        ):
            if stop_at.endswith("-io"):
                raise OSError("Received reader result could not be accepted")
            raise LockCancelled("Stopped before accepting the received result")
        return save(self, key, *args, **kwargs)

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            fault.setattr(CompileWork, "save", interrupted)
            try:
                first = await generate_artifact(root, options, scope=scope, context=context)
            except LockCancelled:
                assert not stop_at.endswith("-io")
            else:
                assert stop_at.endswith("-io") and first.status == "blocked", first
                assert first.generation_id is None
        before = len(service.requests)
        if not search:
            other = ExecutionContext(storage_identity=identity)
            with begin_storage_operation(root, other, "unrelated-artifact"):
                artifacts = ArtifactStore(root, identity)
                artifacts.save(
                    "output/other.txt",
                    b"Unrelated",
                    expected_epoch=artifacts.epoch(),
                    execution_id=other.storage_execution.execution_id,
                    evidence_publication_id=scope.publication_id,
                )
                other._storage_result = "completed"
        resumed = ExecutionContext(
            resume_storage_execution_id=context.storage_execution.execution_id
        )
        result = await generate_artifact(root, options, scope=scope, context=resumed)
        assert kinds.count("rank") == (1 if search else 0), kinds
        assert len(service.requests) == (4 if search else before)
        assert result.status == ("completed" if search else "conflict"), result
