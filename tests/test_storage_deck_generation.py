"""Deck skills use retained evidence and accept their actual generated files."""

import json

import pytest
from stream_transport import StreamTransport
from test_deck_validator import GOOD_DECK
from test_storage_generators import generator_knowledge

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.generators import GenerationOptions, generate_artifact, preview_generation
from ultrakb.application.views import storage_scope
from ultrakb.config import save_config
from ultrakb.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


def deck_knowledge(tmp_path):
    root, identity = generator_knowledge(tmp_path)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 12, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            "role_generation": {"answer": {"max_tokens": 128}},
        },
    )
    return root, identity


def tool_response(name, arguments, call_id):
    return {
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("critique", [False, True])
async def test_deck_generation_and_optional_critique_retain_exact_outputs(tmp_path, critique):
    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    target = "output/decks/value/index.html"
    patched = GOOD_DECK.replace("Title", "Reviewed title")
    responses = [
        tool_response("read_file", {"path": "concepts/value.md"}, "read-value"),
        tool_response("write_file", {"path": target, "content": GOOD_DECK}, "write-deck"),
        {"content": "Deck delivered."},
    ]
    if critique:
        responses += [
            tool_response("read_output_or_skill_file", {"path": target}, "read-deck"),
            tool_response("write_file", {"path": target, "content": patched}, "patch-deck"),
            {"content": "Deck reviewed."},
        ]
    context = ExecutionContext()
    options = GenerationOptions("deck", "value", "Present the recorded value.", critique=critique)
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        preview = preview_generation(root, "deck", "value", scope=scope)
        assert preview.target == root / "output/decks/value"
        result = await generate_artifact(root, options, scope=scope, context=context)
        assert result.status == "completed", result
        assert result.validation is not None and result.validation.ok
        assert result.artifact_path == root / target
        assert result.output_dir == root / "output/decks/value"
        assert (
            ArtifactStore(root, identity).read(target)
            == (patched if critique else GOOD_DECK).encode()
        )
        assert storage_scope(root, identity).publication_id == scope.publication_id
        assert not (root / "wiki").exists()
        assert any(
            item["role"] == "tool" and "The recorded value is 17." in item["content"]
            for item in service.requests[1]["messages"]
        )
        if critique:
            assert any(
                item["role"] == "tool" and GOOD_DECK in item["content"]
                for item in service.requests[4]["messages"]
            )
        (root / ".env").unlink()
        replay = await generate_artifact(root, options, scope=scope, context=context)
        assert replay == result
        assert len(service.requests) == len(responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["output/{slug}.html", "wiki/explorations/{slug}.html"])
async def test_custom_deck_skill_retains_all_allowed_outputs_and_publishes_notes(
    tmp_path, template
):
    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    skill = root / "skills/custom/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: Generate a custom artifact.\nod:\n"
        f"  output_path_template: '{template}'\n---\nWrite the requested artifact and notes.\n"
    )
    target = template.format(slug="value")
    outputs = {
        target: "<html>Recorded value 17</html>",
        "wiki/explorations/evidence.md": "# Evidence\n\nThe recorded value is 17.\n",
        "output/notes/evidence.md": "Retain this generated attachment.\n",
    }
    responses = [
        tool_response("write_file", {"path": path, "content": body}, f"write-{index}")
        for index, (path, body) in enumerate(outputs.items())
    ] + [{"content": "Custom artifact delivered."}]
    context = ExecutionContext()
    options = GenerationOptions("deck", "value", "Present the value.", skill_name="custom")
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        preview = preview_generation(root, "deck", "value", skill_name="custom", scope=scope)
        assert preview.target == root / target and not preview.exists
        result = await generate_artifact(root, options, scope=scope, context=context)
        assert result.status == "completed", result
        assert result.validation is None
        assert result.artifact_path == root / target
        assert result.output_dir == (root / target).parent
        current = storage_scope(root, identity)
        assert result.publication_id == current.publication_id != scope.publication_id
        for path, body in outputs.items():
            if path.startswith("wiki/"):
                assert current.knowledge.read(path.removeprefix("wiki/")) == body.encode()
                with pytest.raises(FileNotFoundError):
                    scope.knowledge.read(path.removeprefix("wiki/"))
            else:
                assert ArtifactStore(root, identity).read(path) == body.encode()
            assert str(root / path) in result.resources
        (root / ".env").unlink()
        skill.unlink()
        replay = await generate_artifact(root, options, scope=scope, context=context)
        assert replay.publication_id == result.publication_id
        assert len(service.requests) == len(responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["archive", "overlay"])
@pytest.mark.parametrize("template", ["output/{slug}.html", "wiki/explorations/{slug}.html"])
async def test_custom_file_replacement_preserves_exact_original_and_selected_policy(
    tmp_path, mode, template
):
    root, identity = deck_knowledge(tmp_path)
    skill = root / "skills/custom/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: Produce an artifact.\nod:\n"
        f"  output_path_template: '{template}'\n---\nWrite the artifact.\n"
    )
    target = template.format(slug="value")
    responses = [
        tool_response("write_file", {"path": target, "content": "Original"}, "write-original"),
        {"content": "Original delivered."},
        tool_response(
            "write_file", {"path": target, "content": "Replacement"}, "write-replacement"
        ),
        {"content": "Replacement delivered."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await generate_artifact(
            root,
            GenerationOptions("deck", "value", "Original.", skill_name="custom"),
            scope=storage_scope(root, identity),
        )
        assert first.status == "completed", first
        prior = storage_scope(root, identity)
        preview = preview_generation(root, "deck", "value", skill_name="custom", scope=prior)
        assert preview.exists and preview.target == root / target
        second = await generate_artifact(
            root,
            GenerationOptions(
                "deck",
                "value",
                "Replacement.",
                overwrite=mode,
                skill_name="custom",
                version=preview.version,
            ),
            scope=prior,
        )
        assert second.status == "completed", second
        current = storage_scope(root, identity)
        if target.startswith("wiki/"):
            assert prior.knowledge.read(target.removeprefix("wiki/")) == b"Original"
            assert current.knowledge.read(target.removeprefix("wiki/")) == b"Replacement"
        else:
            store = ArtifactStore(root, identity)
            assert store.read(target, epoch=first.artifact_epoch) == b"Original"
            assert store.read(target) == b"Replacement"
        if mode == "archive":
            archive = target + "-workspace/iteration-1"
            assert second.archive_path == root / archive
            saved = archive + "/value.html"
            assert (
                current.knowledge.read(saved.removeprefix("wiki/"))
                if target.startswith("wiki/")
                else ArtifactStore(root, identity).read(saved)
            ) == b"Original"
        else:
            assert second.archive_path is None
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_critic_checkpoint_recovery_reuses_paid_responses_and_frozen_skill(tmp_path):
    from ultrakb.storage.coordination import storage_stage

    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    skill = root / "skills/custom/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: Produce a deck.\nod:\n  mode: deck\n"
        "  output_path_template: 'output/decks/{slug}/index.html'\n---\nProduce a deck.\n"
    )
    target = "output/decks/value/index.html"
    patched = GOOD_DECK.replace("Title", "Reviewed title")
    responses = [
        tool_response("write_file", {"path": target, "content": GOOD_DECK}, "produce"),
        {"content": "Produced."},
        tool_response("read_output_or_skill_file", {"path": target}, "read-critic"),
        tool_response("write_file", {"path": target, "content": patched}, "patch"),
        {"content": "Reviewed."},
    ]

    def respond(request, index):
        if index == 3:
            with storage_stage(root, identity) as db:
                db.connection.execute(
                    "CREATE TRIGGER fail_critic_accept BEFORE INSERT ON ukb_compile_steps "
                    "WHEN NEW.step_key LIKE 'maintenance-agent:%' "
                    "BEGIN SELECT RAISE(ABORT, 'checkpoint interrupted'); END"
                )
        return responses[index]

    options = GenerationOptions(
        "deck", "value", "Present value.", skill_name="custom", critique=True
    )
    context = ExecutionContext()
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await generate_artifact(root, options, scope=scope, context=context)
        assert first.status == "blocked" and first.error_type == "RecoveryRequired", first
        assert len(service.requests) == 4
        with pytest.raises(FileNotFoundError):
            ArtifactStore(root, identity).read(target)
        skill.unlink()
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_critic_accept")
        resumed = ExecutionContext(
            resume_storage_execution_id=context.storage_execution.execution_id
        )
        result = await generate_artifact(root, options, scope=scope, context=resumed)
        assert result.status == "completed", result
        assert ArtifactStore(root, identity).read(target) == patched.encode()
        assert len(service.requests) == 5
        from ultrakb.storage.execution_store import ExecutionStore

        assert (
            ExecutionStore(root, identity).budget(resumed.storage_execution.model_budget_id).used
            == 5
        )


@pytest.mark.asyncio
async def test_note_publication_rollback_keeps_artifact_and_retries_without_model_work(tmp_path):
    from ultrakb.storage.coordination import storage_stage

    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    target = "output/decks/value/index.html"
    note = "wiki/explorations/value.md"
    responses = [
        tool_response("write_file", {"path": target, "content": GOOD_DECK}, "produce"),
        tool_response("write_file", {"path": note, "content": "# Recorded value\n\n17\n"}, "note"),
        {"content": "Produced."},
    ]
    options = GenerationOptions("deck", "value", "Present the value and retain notes.")
    context = ExecutionContext()
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with storage_stage(root, identity) as db:
            db.connection.execute(
                "CREATE TRIGGER fail_note_publish BEFORE INSERT ON ukb_publications "
                "BEGIN SELECT RAISE(ABORT, 'publication interrupted'); END"
            )
        first = await generate_artifact(root, options, scope=scope, context=context)
        assert first.status == "failed" and first.candidate_revision_id is not None, first
        assert str(root / target) in first.resources and str(root / note) not in first.resources
        assert first.publication_id is None
        assert ArtifactStore(root, identity).read(target) == GOOD_DECK.encode()
        assert storage_scope(root, identity).publication_id == scope.publication_id
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_note_publish")
        resumed = await generate_artifact(root, options, scope=scope, context=context)
        assert resumed.status == "completed", resumed
        assert resumed.candidate_revision_id == first.candidate_revision_id
        assert (
            storage_scope(root, identity).knowledge.read("explorations/value.md")
            == b"# Recorded value\n\n17\n"
        )
        assert str(root / note) in resumed.resources
        assert len(service.requests) == 3


@pytest.mark.asyncio
async def test_published_deck_note_survives_projection_failure_and_replays_without_settings(
    tmp_path, monkeypatch
):
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    target, note = "output/decks/value/index.html", "wiki/explorations/value.md"
    responses = [
        tool_response("write_file", {"path": target, "content": GOOD_DECK}, "produce"),
        tool_response("write_file", {"path": note, "content": "# Value\n\n17\n"}, "note"),
        {"content": "Produced."},
    ]
    context = ExecutionContext()
    options = GenerationOptions("deck", "value", "Present the value and retain notes.")

    def interrupted(*args, **kwargs):
        raise OSError("Wiki file synchronization unavailable")

    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            fault.setattr(ProjectionStore, "sync", interrupted)
            first = await generate_artifact(root, options, scope=scope, context=context)
            assert first.status == "completed" and first.projection_pending, first
            assert first.publication_id == storage_scope(root, identity).publication_id
            assert (
                storage_scope(root, identity).knowledge.read("explorations/value.md")
                == b"# Value\n\n17\n"
            )
            assert str(root / note) in first.resources
            assert not (root / note).exists()
            assert (root / target).read_text() == GOOD_DECK
        (root / ".env").unlink()
        replay = await generate_artifact(root, options, scope=scope, context=context)
        assert replay.status == "completed" and not replay.projection_pending, replay
        assert replay.publication_id == first.publication_id
        assert (root / note).read_text() == "# Value\n\n17\n"
        assert len(service.requests) == 3


@pytest.mark.asyncio
async def test_deck_tools_preserve_existing_other_outputs_and_archived_versions(tmp_path):
    from ultrakb.application.storage_execution import begin_storage_operation

    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    store = ArtifactStore(root, identity)
    protected = {
        "output/notes/other.md": b"An independently retained note.",
        "output/decks/previous-workspace/iteration-1/index.html": b"An immutable old deck.",
    }
    context = ExecutionContext(storage_identity=identity)
    with begin_storage_operation(root, context, "seed-protected-artifacts"):
        for path, payload in protected.items():
            store.save(
                path,
                payload,
                expected_epoch=store.epoch(),
                execution_id=context.storage_execution.execution_id,
                evidence_publication_id=scope.publication_id,
            )
        context._storage_result = "completed"
    target = "output/decks/value/index.html"
    responses = [
        tool_response(
            "write_file", {"path": path, "content": "Unexpected rewrite"}, f"overwrite-{index}"
        )
        for index, path in enumerate(protected)
    ] + [
        tool_response("write_file", {"path": target, "content": GOOD_DECK}, "produce"),
        {"content": "Produced."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await generate_artifact(
            root,
            GenerationOptions("deck", "value", "Present the recorded value."),
            scope=scope,
        )
        assert result.status == "completed", result
        assert set(result.quality) == {"history_write_denied", "unconfirmed_output_write_denied"}
        for path, payload in protected.items():
            assert store.read(path) == payload
            assert str(root / path) not in result.resources
        assert store.read(target) == GOOD_DECK.encode()
        assert len(service.requests) == 4
