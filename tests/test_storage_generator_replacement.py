"""Artifact replacement keeps explicit history and resumes without repeating paid work."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_generators import generator_knowledge

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.generators import GenerationOptions, generate_artifact, preview_generation
from ultrakb.application.views import storage_scope
from ultrakb.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


def skill_body(value):
    return (
        "---\nname: explain-value\ndescription: Explain the recorded value.\n---\n"
        f"# Value\n\n{value}\n"
    )


def skill_responses(value, *, extra=False):
    files = {"SKILL.md": skill_body(value)}
    if extra:
        files["references/original.md"] = "Retain this original reference.\n"
    return [
        {
            "tool_calls": [
                {
                    "id": f"write-{index}",
                    "type": "function",
                    "function": {
                        "name": "write_skill_file",
                        "arguments": json.dumps({"path": path, "content": body}),
                    },
                }
                for index, (path, body) in enumerate(files.items())
            ]
        },
        {"content": "Skill delivered."},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["archive", "overlay"])
async def test_replacement_retains_previous_epoch_and_uses_the_selected_policy(tmp_path, mode):
    root, identity = generator_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    with StreamTransport(
        skill_responses("Original", extra=True) + skill_responses("Replacement")
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await generate_artifact(
            root,
            GenerationOptions("skill", "explain-value", "Explain the original value."),
            scope=scope,
        )
        assert first.status == "completed", first
        old_epoch = first.artifact_epoch
        preview = preview_generation(root, "skill", "explain-value", scope=scope)
        second = await generate_artifact(
            root,
            GenerationOptions(
                "skill",
                "explain-value",
                "Explain the replacement value.",
                overwrite=mode,
                version=preview.version,
            ),
            scope=scope,
        )
        assert second.status == "completed", second
        store = ArtifactStore(root, identity)
        assert (
            store.read("output/skills/explain-value/SKILL.md") == skill_body("Replacement").encode()
        )
        assert (
            store.read("output/skills/explain-value/SKILL.md", epoch=old_epoch)
            == skill_body("Original").encode()
        )
        if mode == "archive":
            assert second.archive_path is not None
            archive = second.archive_path.relative_to(root).as_posix()
            assert archive == "output/skills/explain-value-workspace/iteration-1"
            assert store.read(archive + "/SKILL.md") == skill_body("Original").encode()
            assert b"references/original.md" in store.read(archive + "/diff.md")
            assert (
                store.read(archive + "/references/original.md")
                == b"Retain this original reference.\n"
            )
            with pytest.raises(FileNotFoundError):
                store.read("output/skills/explain-value/references/original.md")
            assert not (root / "output/skills/explain-value/references/original.md").exists()
        else:
            assert second.archive_path is None
            assert (
                store.read("output/skills/explain-value/references/original.md")
                == b"Retain this original reference.\n"
            )
        assert (
            store.read("output/skills/explain-value/references/original.md", epoch=old_epoch)
            == b"Retain this original reference.\n"
        )
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_archive_commits_before_model_work_and_recovers_a_failed_removal(tmp_path):
    from ultrakb.storage.coordination import storage_stage

    root, identity = generator_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    with StreamTransport(
        skill_responses("Original", extra=True) + skill_responses("Replacement")
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        await generate_artifact(
            root,
            GenerationOptions("skill", "explain-value", "Explain the original value."),
            scope=scope,
        )
        preview = preview_generation(root, "skill", "explain-value", scope=scope)
        options = GenerationOptions(
            "skill",
            "explain-value",
            "Explain replacement.",
            overwrite="archive",
            version=preview.version,
        )
        context = ExecutionContext()
        with storage_stage(root, identity) as db:
            db.connection.execute(
                "CREATE TRIGGER fail_removal BEFORE INSERT ON ukb_artifact_revisions "
                "WHEN NEW.operation = 'delete' "
                "BEGIN SELECT RAISE(ABORT, 'removal interrupted'); END"
            )
        first = await generate_artifact(root, options, scope=scope, context=context)
        assert first.status == "failed" and first.archive_path is not None, first
        assert len(service.requests) == 2
        archive = first.archive_path.relative_to(root).as_posix()
        assert (
            ArtifactStore(root, identity).read(archive + "/SKILL.md")
            == skill_body("Original").encode()
        )
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_removal")
        second = await generate_artifact(root, options, scope=scope, context=context)
        assert second.status == "completed" and second.archive_path == first.archive_path, second
        assert len(service.requests) == 4
        (root / ".env").unlink()
        replay = await generate_artifact(root, options, scope=scope, context=context)
        assert replay.status == "completed" and replay.archive_path == second.archive_path
        assert len(service.requests) == 4


@pytest.mark.asyncio
async def test_deleted_first_entry_does_not_hide_an_existing_skill_directory(tmp_path):
    from ultrakb.application.storage_execution import begin_storage_operation

    root, identity = generator_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    store = ArtifactStore(root, identity)
    context = ExecutionContext(storage_identity=identity)
    with begin_storage_operation(root, context, "seed-artifacts"):
        for path, payload in (
            ("output/skills/explain-value/SKILL.md", skill_body("Original").encode()),
            ("output/skills/explain-value/references/keep.md", b"Keep this reference."),
            ("output/skills/explain-value/SKILL.md", None),
        ):
            store.save(
                path,
                payload,
                expected_epoch=store.epoch(),
                execution_id=context.storage_execution.execution_id,
                evidence_publication_id=scope.publication_id,
            )
        context._storage_result = "completed"
    preview = preview_generation(root, "skill", "explain-value", scope=scope)
    assert preview.exists
    with StreamTransport([]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await generate_artifact(
            root, GenerationOptions("skill", "explain-value", "Explain original."), scope=scope
        )
        assert result.status == "conflict", result
        assert not service.requests


@pytest.mark.asyncio
async def test_archive_conflict_does_not_claim_an_unaccepted_archive_or_send_models(
    tmp_path, monkeypatch
):
    from ultrakb.application.storage_execution import begin_storage_operation

    root, identity = generator_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    with StreamTransport(skill_responses("Original")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        await generate_artifact(
            root, GenerationOptions("skill", "explain-value", "Explain original."), scope=scope
        )
        preview = preview_generation(root, "skill", "explain-value", scope=scope)
        save, changed = ArtifactStore.save, []

        def concurrent_write(self, *args, **options):
            if "-workspace/" in options["path"] and not changed:
                changed.append(True)
                other = ExecutionContext(storage_identity=identity)
                with begin_storage_operation(root, other, "concurrent-artifact"):
                    save(
                        self,
                        "output/other.txt",
                        b"Another saved artifact",
                        expected_epoch=self.epoch(),
                        execution_id=other.storage_execution.execution_id,
                        evidence_publication_id=scope.publication_id,
                    )
                    other._storage_result = "completed"
            return save(self, *args, **options)

        with monkeypatch.context() as race:
            race.setattr(ArtifactStore, "save", concurrent_write)
            result = await generate_artifact(
                root,
                GenerationOptions(
                    "skill",
                    "explain-value",
                    "Replace original.",
                    overwrite="archive",
                    version=preview.version,
                ),
                scope=scope,
            )
        assert result.status == "conflict", result
        assert result.archive_path is None and not result.resources, result
        assert (
            not ArtifactStore(root, identity)
            .files(prefix="output/skills/explain-value-workspace/")
            .files
        )
        assert (
            ArtifactStore(root, identity).read("output/skills/explain-value/SKILL.md")
            == skill_body("Original").encode()
        )
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_archive_deletion_retries_directory_sync_after_main_generation_finishes(
    tmp_path, monkeypatch
):
    from ultrakb import locks

    root, identity = generator_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    removed = root / "output/skills/explain-value/references/original.md"
    directory_sync, failures = locks._fsync_directory, []

    def fail_removed_parent(path):
        if path == removed.parent and not removed.exists():
            failures.append(path)
            raise OSError("Directory sync remains unavailable")
        return directory_sync(path)

    with StreamTransport(
        skill_responses("Original", extra=True) + skill_responses("Replacement")
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        await generate_artifact(
            root, GenerationOptions("skill", "explain-value", "Explain original."), scope=scope
        )
        options = GenerationOptions(
            "skill", "explain-value", "Replace original.", overwrite="archive"
        )
        context = ExecutionContext()
        with monkeypatch.context() as fault:
            fault.setattr(locks, "_fsync_directory", fail_removed_parent)
            result = await generate_artifact(root, options, scope=scope, context=context)
            assert result.status == "completed" and result.projection_pending, result
            assert not removed.exists()
            (root / ".env").unlink()
            repeated = await generate_artifact(root, options, scope=scope, context=context)
            assert repeated.status == "completed" and repeated.projection_pending, repeated
            assert len(failures) >= 2
        recovered = await generate_artifact(root, options, scope=scope, context=context)
        assert recovered.status == "completed" and not recovered.projection_pending, recovered
        assert recovered.archive_path == result.archive_path
        assert (root / "output/skills/explain-value/SKILL.md").read_text() == skill_body(
            "Replacement"
        )
        assert len(service.requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["model", "checkpoint"])
async def test_failed_generation_after_archive_preserves_its_actual_execution_state(
    tmp_path, failure
):
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity = generator_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    replies = skill_responses("Original") + (
        [{"error": {"message": "Generation failed", "type": "offline_error"}}]
        if failure == "model"
        else skill_responses("Replacement")
    )

    def respond(request, index):
        if index == 2 and failure == "checkpoint":
            with storage_stage(root, identity) as db:
                db.connection.execute(
                    "CREATE TRIGGER fail_agent_accept BEFORE INSERT ON ukb_compile_steps "
                    "WHEN NEW.step_key LIKE 'maintenance-agent:%' "
                    "BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END"
                )
        return replies[index]

    statuses = [200, 200, 400] if failure == "model" else None
    with StreamTransport(respond, statuses=statuses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        await generate_artifact(
            root, GenerationOptions("skill", "explain-value", "Explain original."), scope=scope
        )
        options = GenerationOptions(
            "skill", "explain-value", "Replace original.", overwrite="archive"
        )
        context = ExecutionContext()
        result = await generate_artifact(root, options, scope=scope, context=context)
        assert result.status == ("failed" if failure == "model" else "blocked"), result
        assert result.archive_path is not None and result.validation is None
        assert (
            ExecutionStore(root, identity).status(context.storage_execution.execution_id)
            == "failed"
        )
        assert len(service.requests) == 3
        if failure == "checkpoint":
            with storage_stage(root, identity) as db:
                db.connection.execute("DROP TRIGGER fail_agent_accept")
            resumed = await generate_artifact(root, options, scope=scope, context=context)
            assert resumed.status == "completed" and resumed.archive_path == result.archive_path
            assert len(service.requests) == 4
