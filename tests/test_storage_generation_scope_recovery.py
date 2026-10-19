"""Generation recovery retains its original publication after the live head advances."""

import pytest
from stream_transport import StreamTransport
from test_deck_validator import GOOD_DECK
from test_storage_deck_generation import deck_knowledge, tool_response

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.generators import GenerationOptions, generate_artifact
from ultrakb.application.views import published_scope, storage_scope
from ultrakb.desktop.verification_data import publish_pages
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.generation_store import GenerationStore, generation_request

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["before-plan", "before-save", "before-save-no-key"])
async def test_default_generation_resume_keeps_its_paid_work_and_original_publication(
    tmp_path, monkeypatch, interruption
):
    root, identity = deck_knowledge(tmp_path)
    before = published_scope(root).publication_id
    path = "output/decks/value/index.html"
    replies = [
        tool_response("write_file", {"path": path, "content": GOOD_DECK}, "deliver"),
        {"content": "Delivered."},
    ]
    context = ExecutionContext()
    options = GenerationOptions("deck", "value", "Present the recorded value.")

    def interrupted(*args, **kwargs):
        raise OSError("Local persistence interrupted after the model completed")

    with StreamTransport(lambda request, index: replies[index % 2]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            fault.setattr(
                GenerationStore if interruption == "before-plan" else ArtifactStore,
                "prepare" if interruption == "before-plan" else "save",
                interrupted,
            )
            first = await generate_artifact(root, options, context=context)
        assert first.status == "failed", first
        assert len(service.requests) == 2
        publish_pages(root, {"concepts/later.md": b"A later, independent publication."})
        newer = published_scope(root).publication_id
        assert newer != before
        resumed_context = ExecutionContext(
            resume_storage_execution_id=context.storage_execution.execution_id
        )
        if interruption == "before-save-no-key":
            (root / ".env").unlink()
        resumed = await generate_artifact(root, options, context=resumed_context)
        assert resumed.status == "completed", resumed
        assert len(service.requests) == 2, "A later head must not cause another paid generation"
    assert ArtifactStore(root, identity).read(path) == GOOD_DECK.encode()
    original_scope = storage_scope(root, identity, publication_id=before)
    retained = GenerationStore(root, identity).find(
        resumed_context.storage_execution.model_budget_id,
        generation_request(original_scope, options),
    )
    assert retained.publication_id == before
    assert published_scope(root).publication_id == newer


@pytest.mark.asyncio
@pytest.mark.parametrize("advance_head", [False, True])
async def test_distinct_generation_units_do_not_reuse_another_units_paid_result(
    tmp_path, advance_head
):
    root, identity = deck_knowledge(tmp_path)
    context = ExecutionContext(usage_task_id="c" * 32, usage_unit_id="0")
    options = GenerationOptions("deck", "value", "Present the recorded value.", overwrite="overlay")
    path = "output/decks/value/index.html"
    replacement = GOOD_DECK.replace("</body>", "<p>Second independent unit.</p></body>")
    responses = [
        tool_response("write_file", {"path": path, "content": body}, f"write-{index}")
        if index % 2 == 0
        else {"content": "Delivered."}
        for index, body in enumerate((GOOD_DECK, GOOD_DECK, replacement, replacement))
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await generate_artifact(root, options, context=context)
        assert first.status == "completed", first
        budget = context.storage_execution.model_budget_id
        if advance_head:
            publish_pages(root, {"concepts/later.md": b"Independent, later knowledge."})
        context.usage_unit_id = "1"
        second = await generate_artifact(root, options, context=context)
        assert second.status == "completed", second
        assert len(service.requests) == 4
        assert context.storage_execution.model_budget_id == budget
    assert ArtifactStore(root, identity).read(path) == replacement.encode()


@pytest.mark.asyncio
async def test_reused_paid_context_blocks_when_its_input_binding_is_unavailable(
    tmp_path, monkeypatch
):
    from ultrakb.storage.checkpoint_store import CompileCheckpointStore

    root, _ = deck_knowledge(tmp_path)
    context = ExecutionContext()
    options = GenerationOptions("deck", "value", "Present the recorded value.")
    responses = [
        tool_response(
            "write_file", {"path": "output/decks/value/index.html", "content": GOOD_DECK}, "write"
        ),
        {"content": "Delivered."},
    ]

    def interrupted(*args, **kwargs):
        raise OSError("Interrupted before the generation plan was retained")

    with StreamTransport(lambda request, index: responses[index % 2]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            fault.setattr(GenerationStore, "prepare", interrupted)
            first = await generate_artifact(root, options, context=context)
        assert first.status == "failed", first
        assert len(service.requests) == 2
        publish_pages(root, {"concepts/later.md": b"A later independent publication."})
        with monkeypatch.context() as fault:
            fault.setattr(CompileCheckpointStore, "find_work", lambda *args, **kwargs: None)
            retry = await generate_artifact(root, options, context=context)
        assert retry.status == "blocked", retry
        assert retry.error_type == "RecoveryRequired"
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_cancelling_local_generation_recovery_keeps_its_committed_result(
    tmp_path, monkeypatch
):
    import asyncio
    from threading import Event

    root, identity = deck_knowledge(tmp_path)
    context = ExecutionContext()
    options = GenerationOptions("deck", "value", "Present the recorded value.")
    path = "output/decks/value/index.html"
    responses = [
        tool_response("write_file", {"path": path, "content": GOOD_DECK}, "write"),
        {"content": "Delivered."},
    ]

    def interrupted(*args, **kwargs):
        raise OSError("Interrupted before artifact acceptance")

    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            fault.setattr(ArtifactStore, "save", interrupted)
            first = await generate_artifact(root, options, context=context)
        assert first.status == "failed", first
        accepted, release = Event(), Event()
        original_sync = ArtifactStore.sync

        def pause_after_acceptance(self, *args, **kwargs):
            result = original_sync(self, *args, **kwargs)
            accepted.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(ArtifactStore, "sync", pause_after_acceptance)
        recovery = asyncio.create_task(
            generate_artifact(
                root,
                options,
                context=ExecutionContext(
                    resume_storage_execution_id=context.storage_execution.execution_id
                ),
            )
        )
        try:
            assert await asyncio.to_thread(accepted.wait, 5)
            recovery.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        result = await recovery
        assert result.status == "completed" and result.generation_id is not None, result
        assert len(service.requests) == 2
    assert ArtifactStore(root, identity).read(path) == GOOD_DECK.encode()


@pytest.mark.parametrize("prefix_completed", [False, True])
def test_native_task_retry_keeps_original_unit_and_head_after_local_acceptance_failure(
    tmp_path, prefix_completed
):
    from ultrakb.application.generators import preview_generation
    from ultrakb.runtime.requests import DeleteConversation, GenerateArtifact
    from ultrakb.runtime.task_actions import preview_retry, retry_task
    from ultrakb.runtime.tasks import TaskManager
    from ultrakb.storage.coordination import storage_stage

    root, identity = deck_knowledge(tmp_path)
    version = preview_generation(root, "deck", "value").version
    path = "output/decks/value/index.html"
    responses = [
        tool_response("write_file", {"path": path, "content": GOOD_DECK}, "write"),
        {"content": "Delivered."},
    ]
    requests = [GenerateArtifact("deck", "value", "Present value.", version)]
    expected_sends = 2
    if prefix_completed:
        from conversation_fixtures import seed_conversation

        prior = seed_conversation(root, [("An earlier note.", "Recorded.")])
        requests.insert(0, DeleteConversation(prior.id, prior.version))
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER interrupt_artifact BEFORE INSERT ON ukb_artifact_revisions "
            "WHEN NEW.path = 'output/decks/value/index.html' "
            "BEGIN SELECT RAISE(ABORT, 'Local acceptance failed'); END"
        )
    manager = TaskManager(history_dir=tmp_path / "history", max_workers=1)
    try:
        with StreamTransport(responses) as service:
            (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
            first_id = manager.submit(root, requests)
            first = manager.wait(first_id, timeout=30)
            assert first.state == ("partial" if prefix_completed else "failed"), first
            assert first.results[-1].generation["execution_id"]
            assert len(service.requests) == expected_sends, first.results[-1]
            publish_pages(root, {"concepts/later.md": b"Independent later knowledge."})
            newer = published_scope(root).publication_id
            (root / ".env").unlink()
            again_id = retry_task(manager, preview_retry(manager, first_id))
            again = manager.wait(again_id, timeout=30)
            assert again.state == "failed", again
            assert again.results[0].generation["execution_id"]
            assert len(service.requests) == expected_sends
            with storage_stage(root, identity) as db:
                db.connection.execute("DROP TRIGGER interrupt_artifact")
            (root / "wiki").rename(tmp_path / "discarded-projection")
            retried = manager.wait(
                retry_task(manager, preview_retry(manager, again_id)), timeout=30
            )
            assert retried.state == "completed", retried
            assert len(service.requests) == expected_sends
            assert retried.results[0].generation["execution_id"]
            assert retried.results[0].model_usage["current"]["actual_sends"] == 0
        assert ArtifactStore(root, identity).read(path) == GOOD_DECK.encode()
        assert published_scope(root).publication_id == newer
    finally:
        manager.shutdown(stop=True)
        assert manager.join(5)


@pytest.mark.asyncio
async def test_replay_by_application_unit_returns_the_original_execution_receipt(tmp_path):
    root, identity = deck_knowledge(tmp_path)
    task_id = "d" * 32
    context = ExecutionContext(usage_task_id=task_id, usage_unit_id="3")
    options = GenerationOptions("deck", "value", "Present value.")
    responses = [
        tool_response(
            "write_file", {"path": "output/decks/value/index.html", "content": GOOD_DECK}, "write"
        ),
        {"content": "Delivered."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await generate_artifact(root, options, context=context)
        assert first.status == "completed", first
        publish_pages(root, {"concepts/later.md": b"Independent later publication."})
        replay = await generate_artifact(
            root, options, context=ExecutionContext(usage_task_id=task_id, usage_unit_id="3")
        )
        assert replay.status == "completed", replay
        assert replay.generation_id == first.generation_id
        assert replay.execution_id == first.execution_id
        assert replay.model_usage["current"]["actual_sends"] == 2
        assert len(service.requests) == 2
    assert ArtifactStore(root, identity).read("output/decks/value/index.html") == GOOD_DECK.encode()


@pytest.mark.asyncio
async def test_second_generation_unit_observes_cancellation_with_its_existing_executor(tmp_path):
    import asyncio
    from contextlib import suppress
    from threading import Event

    from ultrakb.locks import LockCancelled

    root, _ = deck_knowledge(tmp_path)
    context = ExecutionContext(usage_task_id="f" * 32, usage_unit_id="0")
    options = GenerationOptions("deck", "value", "Present value.", overwrite="overlay")
    entered, release = Event(), Event()
    responses = [
        tool_response(
            "write_file", {"path": "output/decks/value/index.html", "content": GOOD_DECK}, "write"
        ),
        {"content": "Delivered."},
    ]

    def respond(request, index):
        if index == 2:
            entered.set()
            assert release.wait(5)
        return responses[index % 2]

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = await generate_artifact(root, options, context=context)
        assert first.status == "completed", first
        executor = context.executor
        context.usage_unit_id = "1"
        second = asyncio.create_task(generate_artifact(root, options, context=context))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            second.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with suppress(asyncio.CancelledError, LockCancelled):
            await second
        assert context.executor is executor
        assert len(service.requests) == 3, "Stopping unit 1 must prevent its next model request"


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["before-plan", "before-save"])
async def test_backup_restore_resumes_paid_generation_under_a_new_execution(
    tmp_path, monkeypatch, interruption
):
    from ultrakb.application.storage_maintenance import backup_knowledge, restore_knowledge

    root, identity = deck_knowledge(tmp_path)
    path = "output/decks/value/index.html"
    options = GenerationOptions("deck", "value", "Present the recorded value.")
    context = ExecutionContext()
    replies = [
        tool_response("write_file", {"path": path, "content": GOOD_DECK}, "restore-deliver"),
        {"content": "Delivered."},
    ]

    def interrupted(*args, **kwargs):
        raise OSError("Local save interrupted after paid generation")

    with StreamTransport(replies) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            fault.setattr(
                GenerationStore if interruption == "before-plan" else ArtifactStore,
                "prepare" if interruption == "before-plan" else "save",
                interrupted,
            )
            first = await generate_artifact(root, options, context=context)
        assert first.status == "failed", first
        assert len(service.requests) == 2
        previous = context.storage_execution
        backup = tmp_path / "generation-backup"
        backup_knowledge(root, backup)
        destination = tmp_path / "generation-restored"
        restored = restore_knowledge(backup, destination)
        assert restored.identity.generation != identity.generation
        if interruption == "before-save":
            (destination / ".env").unlink()
        resumed_context = ExecutionContext(resume_storage_execution_id=previous.execution_id)
        resumed = await generate_artifact(destination, options, context=resumed_context)
        assert resumed.status == "completed", resumed
        assert len(service.requests) == 2, "Restoring a paid plan must not send it again"
    assert resumed_context.storage_execution.execution_id != previous.execution_id
    assert resumed_context.storage_execution.model_budget_id == previous.model_budget_id
    assert ArtifactStore(destination, restored.identity).read(path) == GOOD_DECK.encode()
