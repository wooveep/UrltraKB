"""Public question operations use fixed knowledge and independent model receipts."""

import json

import pytest
from error_transport import ErrorTransport

from ultrakb.application.conversations import ask_question
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.views import storage_scope
from ultrakb.config import save_config
from ultrakb.storage.database import create_database
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.receipt_store import ReceiptStore
from ultrakb.storage.usage_store import UsageStore


def question_knowledge(tmp_path):
    root = tmp_path / "kb"
    identity = create_database(root)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 4, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            "role_generation": {"answer": {"max_tokens": 128}},
        },
    )
    knowledge = KnowledgeStore(root, identity)
    publication = knowledge.publish(
        knowledge.prepare_revision(
            {"index.md": b"Read sources/fixture.md", "sources/fixture.md": b"The value is 17."},
            expected=None,
        )
    )
    return root, identity, publication


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_encoding", [False, True])
async def test_public_question_keeps_the_actual_error_receipt_without_any_legacy_ledger(
    tmp_path, invalid_encoding
):
    root, identity, publication = question_knowledge(tmp_path)
    context = ExecutionContext(storage_identity=identity)
    with ErrorTransport(
        status=400, headers={"Content-Encoding": "gzip"} if invalid_encoding else None
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root, "Read the recorded value.", scope=storage_scope(root, identity), context=context
        )
        assert len(service.requests) == 1, (result.status, result.error)
    assert result.status == "failed" and result.answer == ""
    assert result.model_usage["current"]["actual_sends"] == 1
    assert result.usage == {
        "input_cached": None,
        "input_uncached": None,
        "output": None,
        "reasoning": None,
    }
    execution = context.storage_execution
    [request] = UsageStore(root, identity).requests(execution_id=execution.execution_id).records
    assert json.loads(ReceiptStore(root, identity).read(request.request_attempt_id)) == service.body
    assert ExecutionStore(root, identity).status(execution.execution_id) == "failed"
    assert KnowledgeStore(root, identity).bind().publication_id == publication.publication_id


@pytest.mark.asyncio
async def test_question_maps_a_retained_stream_receipt_failure_to_a_repairable_result(tmp_path):
    from stream_transport import StreamTransport

    from ultrakb.storage.coordination import storage_stage

    root, identity, publication = question_knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_question_response BEFORE INSERT ON ukb_response_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected response failure'); END"
        )
    with StreamTransport([{"content": "Received partial answer"}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root, "Read the value.", save=True, scope=storage_scope(root, identity)
        )
    assert result.status == "blocked" and result.model_usage["current"]["actual_sends"] == 1
    assert len(service.requests) == 1 and result.saved_path is None
    assert KnowledgeStore(root, identity).bind().publication_id == publication.publication_id
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_question_response")
    repair = ReceiptStore(root, identity).repair(limit=10)
    assert repair.repaired >= 1 and repair.invalid == repair.remaining == 0
    for name in ("usage", "observations", "model-tasks", "published-knowledge", "knowledge"):
        assert not (root / ".ultrakb" / name).exists()
    assert not (root / "wiki").exists()


@pytest.mark.asyncio
async def test_public_question_reads_its_fixed_source_and_retains_stream_usage(tmp_path):
    from stream_transport import StreamTransport

    root, identity, _ = question_knowledge(tmp_path)
    context = ExecutionContext(storage_identity=identity)
    responses = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "read-source",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sources/fixture.md"}'},
                }
            ]
        },
        {"content": "The recorded value is 17."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root, "Read the recorded value.", scope=storage_scope(root, identity), context=context
        )
    assert result.status == "completed", (result.error, result.unfinished)
    assert result.answer == "The recorded value is 17."
    assert any(
        message["role"] == "tool" and "The value is 17." in str(message["content"])
        for message in service.requests[1]["messages"]
    )
    assert result.model_usage["current"]["actual_sends"] == 2
    assert result.usage == {"input_cached": 0, "input_uncached": 240, "output": 14, "reasoning": 0}
    requests = (
        UsageStore(root, identity)
        .requests(execution_id=context.storage_execution.execution_id)
        .records
    )
    assert [
        ReceiptStore(root, identity).read(request.request_attempt_id) for request in requests
    ] == service.bodies
    assert all(request.observation == "transport" for request in requests)


@pytest.mark.asyncio
async def test_saved_answers_publish_unique_pages_while_the_worktree_is_paused(tmp_path):
    from pathlib import Path

    import yaml
    from stream_transport import StreamTransport

    from ultrakb.application.wiki_worktree import begin_external_edit
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, _ = question_knowledge(tmp_path)
    assert ProjectionStore(root, identity).sync().status == "synced"
    begin_external_edit(root, identity)
    draft = root / "wiki/index.md"
    draft.write_text("Unaccepted editor draft")
    question = '解释 "这个问题"\n及其依据'
    with StreamTransport([{"content": "See [[index]] and [[concepts/missing]]."}] * 2) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = await ask_question(root, question, save=True, scope=storage_scope(root, identity))
        assert first.status == "completed", (first.error, first.unfinished)
        second = await ask_question(root, question, save=True, scope=storage_scope(root, identity))
    assert second.status == "completed", (second.error, second.unfinished)
    assert first.saved_path != second.saved_path
    assert first.projection.status == second.projection.status == "paused"
    bound = KnowledgeStore(root, identity).bind()
    for result in (first, second):
        path = Path(result.saved_path).relative_to(root / "wiki").as_posix()
        content = bound.read(path).decode()
        metadata = yaml.safe_load(content.split("---", 2)[1])
        assert metadata["query"] == question
        assert "[[index]]" in content and "[[concepts/missing]]" not in content
        assert result.result_publication_id is not None
        assert result.model_usage["current"]["actual_sends"] == 1
        assert not Path(result.saved_path).exists()
    assert draft.read_text() == "Unaccepted editor draft"
    assert bound.read("log.md").count(b"query | ") == 2


@pytest.mark.asyncio
async def test_question_wait_releases_storage_and_reads_old_evidence_after_a_new_publication(
    tmp_path,
):
    import asyncio
    from threading import Event

    from stream_transport import StreamTransport

    from ultrakb.application.execution_configuration import patch_storage_config
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.locks import file_write_lock
    from ultrakb.storage.blobs import put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.maintenance import Maintenance

    root, identity, first = question_knowledge(tmp_path)
    old = storage_scope(root, identity)
    gate = Event()
    with storage_stage(root, identity) as db:
        put_blob(db, b"unreferenced orphan")
    responses = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "read-old",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sources/fixture.md"}'},
                }
            ]
        },
        {"content": "The recorded value is 17."},
    ]
    with StreamTransport(responses, gate=gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        task = asyncio.create_task(ask_question(root, "Read the value.", save=True, scope=old))
        try:
            with file_write_lock(root / ".ultrakb/ingest.lock"):
                assert await asyncio.to_thread(service.wait_for_requests, 1)
                collected = await asyncio.to_thread(Maintenance(root, identity).collect)
                assert collected.status == "completed" and collected.deleted_count == 1
                await asyncio.to_thread(
                    patch_storage_config,
                    root,
                    identity,
                    KbConfigPatchRequest(kb=str(root), config={"language": "zh"}),
                )
                knowledge = KnowledgeStore(root, identity)
                current = knowledge.publish(
                    knowledge.prepare_revision(
                        {"index.md": b"New", "sources/fixture.md": b"The value is now 99."},
                        expected=first.publication_id,
                    )
                )
        finally:
            gate.set()
            result = await task
    assert result.status == "blocked" and result.answer == "The recorded value is 17."
    assert result.reason_code == "knowledge_changed" and result.candidate_revision_id is not None
    assert result.saved_path is None and result.model_usage["current"]["actual_sends"] == 2
    assert KnowledgeStore(root, identity).bind().publication_id == current.publication_id
    assert any(
        message["role"] == "tool" and "The value is 17." in str(message["content"])
        for message in service.requests[1]["messages"]
    )
    assert all(
        "Answer in en" in str(request["messages"][0]["content"]) for request in service.requests
    )


@pytest.mark.asyncio
async def test_stopped_question_retains_late_stream_usage_without_publishing_a_partial_answer(
    tmp_path,
):
    from threading import Event

    from stream_transport import StreamTransport

    root, identity, publication = question_knowledge(tmp_path)
    stopped, body_gate = Event(), Event()

    def stop_after_delta(event):
        if event.get("event") == "delta":
            stopped.set()
            body_gate.set()

    context = ExecutionContext(
        storage_identity=identity, cancelled=stopped.is_set, on_event=stop_after_delta
    )
    with StreamTransport([{"content": "Partial received answer"}], body_gate=body_gate) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root, "Read the value.", save=True, scope=storage_scope(root, identity), context=context
        )
    assert result.status == "stopped" and result.saved_path is None
    assert (
        result.model_usage["current"]["actual_sends"] == 1
        and result.model_usage["current"]["input_total"] == 120
    )
    execution = context.storage_execution
    [request] = UsageStore(root, identity).requests(execution_id=execution.execution_id).records
    assert request.business_state == "cancelled"
    assert ReceiptStore(root, identity).read(request.request_attempt_id) == service.bodies[0]
    assert ExecutionStore(root, identity).status(execution.execution_id) == "cancelled"
    assert KnowledgeStore(root, identity).bind().publication_id == publication.publication_id


@pytest.mark.asyncio
@pytest.mark.parametrize("same_original", [False, True])
async def test_saved_answer_keeps_old_source_after_its_original_claim_page_is_deleted(
    tmp_path, same_original
):
    from pathlib import Path

    from stream_transport import StreamTransport

    from ultrakb.application.answer_citations import read_answer_reference
    from ultrakb.application.pages import delete_page
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore

    root, identity, first = question_knowledge(tmp_path)
    catalog, knowledge = CatalogStore(root, identity), KnowledgeStore(root, identity)
    original = tmp_path / "original.md"
    body_file = tmp_path / "body.md"
    original.write_text("Frozen unprocessed original")
    fixed_source = catalog.admit(original, source_key="stable-source") if same_original else None
    previous = first.publication_id
    units = []
    claim = b"---\nsources: [original]\n---\nThe original claim was 17."
    for ordinal, body in enumerate(("The value was 17.", "The value is now 99.")):
        body_file.write_text(body)
        if fixed_source is None:
            original.write_text(body)
        source = fixed_source or catalog.admit(original, source_key="stable-source")
        processing = catalog.retain_processing(
            source.revision_id,
            {"body.md": body_file},
            body_path="body.md",
            fingerprint=f"policy-{ordinal}",
            operation_id=str(ordinal),
            decision=classify_markdown_tokens(5),
        )
        unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="original")
        publication = knowledge.publish(
            knowledge.prepare_revision(
                {"index.md": f"Index {ordinal}".encode(), "concepts/claim.md": claim},
                expected=previous,
                unit_revisions=(unit.unit_revision_id,),
            )
        )
        previous = publication.publication_id
        units.append(unit.unit_revision_id)
    responses = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "read-claim",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"concepts/claim.md"}'},
                }
            ]
        },
        {"content": "The original claim was 17. [[concepts/claim]]"},
    ]
    if same_original:
        responses[0]["tool_calls"].append(
            {
                "index": 1,
                "id": "read-new",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"sources/original.md"}'},
            }
        )
        from ultrakb.application.answer_citations import capture_answer_citations
        from ultrakb.application.query_views import read_selected_file, resolve_query_views

        selection = resolve_query_views(root, scope=storage_scope(root, identity))
        view = selection.views[0]
        native_path = "sources/original.md"
        assert read_selected_file(view, native_path) == b"The value is now 99."
        cited = capture_answer_citations(
            "See [[concepts/claim]].",
            selection,
            [
                {
                    "path": native_path,
                    "digest": view.files[native_path],
                    "source_revision_id": view.source_revisions_by_path[native_path],
                    "location": {"chars": "0:19"},
                }
            ],
        )
        assert all(not reference["returned"] for reference in cited["references"])
    context = ExecutionContext(storage_identity=identity)
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root,
            "Explain the old claim.",
            save=True,
            scope=storage_scope(root, identity),
            context=context,
        )
    assert result.status == "completed", result.error
    deleted = delete_page(root, "concepts/claim", scope=storage_scope(root, identity))
    assert deleted["status"] == "deleted"
    current = knowledge.bind()
    path = Path(result.saved_path).relative_to(root / "wiki").as_posix()
    assert b"17" in current.read(path)
    assert current.read_source(units[0], "body.md") == b"The value was 17."
    assert {unit.unit_revision_id for unit in current.input_units(page=path)} == set(units)
    [reference] = result.citations["references"]
    if same_original:
        assert any(read["path"] == "sources/original.md" for read in context.returned_reads)
    assert reference["returned"] is False
    content, _, _ = read_answer_reference(root, reference)
    assert "The original claim was 17." in content


@pytest.mark.asyncio
async def test_partial_stream_receipts_keep_known_details_when_later_events_are_empty(tmp_path):
    from stream_transport import StreamTransport

    from ultrakb.llm_usage_models import UsageReceipt

    root, identity, _ = question_knowledge(tmp_path)
    events = []
    context = ExecutionContext(storage_identity=identity, on_event=events.append)
    partial = [
        {"prompt_tokens": 120, "prompt_tokens_details": {"cached_tokens": 80}},
        {"completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": None}},
    ]
    with StreamTransport([{"content": "A complete answer."}], usage_events=partial) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root, "Read the value.", scope=storage_scope(root, identity), context=context
        )
    receipt = UsageReceipt.model_validate(result.model_usage)
    assert receipt.current.input_total == 120 and receipt.current.output_total == 7
    assert (
        receipt.current.cached_input == 80 and receipt.current.unknown_fields["cached_input"] == 0
    )
    assert receipt.current.unknown_fields["reasoning_output"] == 1
    assert result.usage["reasoning"] is None
    for event in events:
        if event.get("event") == "model_usage_progress":
            UsageReceipt.model_validate(event["data"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["execution_finish", "usage_read"])
async def test_saved_question_remains_saved_when_later_observations_fail(
    tmp_path, monkeypatch, fault
):
    from pathlib import Path

    from stream_transport import StreamTransport

    from ultrakb.storage.coordination import storage_stage

    root, identity, _ = question_knowledge(tmp_path)
    if fault == "execution_finish":
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_execution_finish BEFORE UPDATE ON ukb_executions "
                "WHEN NEW.state != 'started' "
                "BEGIN SELECT RAISE(ABORT, 'injected execution finish'); END"
            )
    else:

        def unavailable(*args, **kwargs):
            raise OSError("injected usage read failure")

        monkeypatch.setattr(UsageStore, "receipt", unavailable)
    with StreamTransport([{"content": "A complete saved answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(
            root, "Read the value.", save=True, scope=storage_scope(root, identity)
        )
    assert result.status == "completed" and result.saved_path is not None
    current = KnowledgeStore(root, identity).bind()
    assert result.result_publication_id == current.publication_id
    path = Path(result.saved_path).relative_to(root / "wiki").as_posix()
    assert b"A complete saved answer." in current.read(path)
    assert result.projection is not None and result.changes == (f"created: wiki/{path}",)
    assert result.reason_code in {"execution_confirmation_pending", "usage_unavailable"}
    assert len(service.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_stop_while_saving_waits_for_the_worker_and_preserves_committed_results(
    tmp_path, monkeypatch, after_commit
):
    import asyncio
    from pathlib import Path
    from threading import Event

    from stream_transport import StreamTransport

    root, identity, original_publication = question_knowledge(tmp_path)
    entered, release = Event(), Event()
    original_publish = KnowledgeStore.publish

    def gated_publish(store, prepared, **kwargs):
        if after_commit:
            publication = original_publish(store, prepared, **kwargs)
        entered.set()
        assert release.wait(3), "The event loop could not release the storage worker"
        return publication if after_commit else original_publish(store, prepared, **kwargs)

    monkeypatch.setattr(KnowledgeStore, "publish", gated_publish)
    context = ExecutionContext(storage_identity=identity)
    with StreamTransport([{"content": "A complete saved answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        task = asyncio.create_task(
            ask_question(
                root,
                "Read the value.",
                save=True,
                scope=storage_scope(root, identity),
                context=context,
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert not task.done(), "Saving blocked the application event loop"
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "Cancellation abandoned the active storage worker"
        finally:
            release.set()
        result = await task
    current = KnowledgeStore(root, identity).bind()
    assert result.model_usage["current"]["actual_sends"] == len(service.requests) == 1
    if after_commit:
        assert result.status == "completed" and result.saved_path is not None
        assert current.publication_id == result.result_publication_id
        path = Path(result.saved_path).relative_to(root / "wiki").as_posix()
        assert b"A complete saved answer." in current.read(path)
    else:
        assert result.status == "stopped" and result.saved_path is None
        assert current.publication_id == original_publication.publication_id
