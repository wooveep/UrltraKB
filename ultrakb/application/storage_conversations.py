"""Continue a main-store session while its actual worker owns the session lease."""

import asyncio
import sqlite3
from contextlib import aclosing
from dataclasses import asdict, replace
from uuid import uuid4

from ultrakb.agent.query import (
    build_chat_agent,
    build_run_config_from_bundle,
    iter_agent_response_events,
)
from ultrakb.agent.query_evidence import restrict_query_agent
from ultrakb.context.workers import run_blocking
from ultrakb.locks import LockCancelled, async_session_lock
from ultrakb.model_tokenizer import ContextCapacityError, question_measure
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.session_store import SessionStore

from .conversations import AnswerResult
from .query_views import resolve_query_views
from .stored_conversation_context import prepare_stored_conversation


async def continue_stored_conversation(
    scope, message, *, session_id, new_session_id, attempt_id, submission_order, context, focus
):
    identity = session_id or new_session_id or uuid4().hex
    result = None

    def worker(bundle):
        async def owned_turn():
            nonlocal result
            result = await _continue(
                scope,
                message,
                identity,
                session_id is not None,
                attempt_id or uuid4().hex,
                submission_order,
                context,
                bundle,
                focus,
            )

        asyncio.run(owned_turn())

    try:
        # Only the lifecycle and execution are owned by the parent. The worker
        # acquires its own session/stage leases; no connection or lock is borrowed.
        with context.begin(scope.kb_dir) as bundle:
            try:
                await run_blocking(lambda: worker(bundle))
            except asyncio.CancelledError:
                if result is None:
                    raise
            assert result is not None
            context._storage_result = (
                "completed"
                if result.status == "completed"
                else "cancelled"
                if result.status == "stopped"
                else "failed"
            )
        return result
    except (Exception, asyncio.CancelledError) as error:
        if result is not None:
            return replace(
                result,
                reason_code=result.reason_code or "execution_confirmation_pending",
                unfinished=(*result.unfinished, "confirm execution cleanup"),
                diagnostics=(*result.diagnostics, "execution_confirmation_pending"),
            )
        return _failed(error, "", identity, 0, "prepare context")


def _failed(error, answer, identity, count, stage, diagnostics=None):
    return AnswerResult(
        "stopped"
        if isinstance(error, (LockCancelled, asyncio.CancelledError))
        else "blocked"
        if isinstance(error, (RecoveryRequired, ContextCapacityError))
        else "failed",
        answer,
        session_id=identity,
        turn_count=count,
        context=diagnostics,
        error=str(error)
        if isinstance(error, ContextCapacityError)
        else f"Conversation did not complete ({type(error).__name__})",
        unfinished=(stage,),
    )


def _committed_result(store, identity, attempt_id, message, execution_id):
    retained = store.completed_submission(identity, attempt_id, message, execution_id=execution_id)
    if retained is None:
        return None
    metadata = retained["metadata"]
    if metadata.get("schema") != 1 or not isinstance(metadata.get("result"), dict):
        raise RecoveryRequired("Committed conversation result metadata needs recovery")
    values = dict(metadata["result"])
    for key in ("resources", "changes", "unfinished", "image_coverage", "diagnostics"):
        values[key] = tuple(values.get(key, ()))
    if values.get("projection") is not None:
        from ultrakb.storage.wiki_projection import ProjectionResult

        values["projection"] = ProjectionResult(**values["projection"])
    return AnswerResult(answer=retained["answer"], **values)


async def _continue(
    scope, message, identity, require_existing, attempt_id, submission_order, context, bundle, focus
):
    assert scope.knowledge is not None
    store = SessionStore(scope.kb_dir, scope.knowledge.identity)
    assert context.executor is not None and context.snapshot is not None
    assert context.storage_execution is not None
    execution_id = context.storage_execution.execution_id
    executor = context.executor
    config = context.snapshot.values()["effective"]
    parts, count, diagnostics, trace = [], 0, None, []
    registered = False
    stage = "prepare context"

    def cancelled():
        context.check_stop()
        return False

    try:
        async with async_session_lock(
            scope.kb_dir,
            identity,
            cancelled=cancelled,
            on_wait=context.waiting,
            database_identity=store.identity,
        ):
            context.check_stop()
            try:
                conversation = store.load(identity)
            except FileNotFoundError:
                if require_existing:
                    raise
                store.create(
                    identity,
                    model=config["model"],
                    language=config["language"],
                    title=" ".join(message.split())[:60],
                    view_id=scope.view_id,
                )
                conversation = store.load(identity)
            if conversation.view_id != scope.view_id:
                raise ValueError("Conversation belongs to another knowledge view")
            count = len(conversation.turns)
            store.start_submission(
                identity,
                attempt_id,
                message,
                execution_id=execution_id,
                publication_id=scope.knowledge.publication_id,
                order=submission_order,
            )
            registered = True
            completed = _committed_result(store, identity, attempt_id, message, execution_id)
            if completed is None and store.recover_submission(
                identity,
                attempt_id,
                message,
                execution_id=execution_id,
                check_cancelled=context.check_stop,
            ):
                completed = _committed_result(store, identity, attempt_id, message, execution_id)
            if completed is not None:
                return replace(
                    completed,
                    usage={"input_cached": 0, "input_uncached": 0, "output": 0, "reasoning": 0},
                    diagnostics=(*completed.diagnostics, "reused_completed_submission"),
                )
            with context.activity("preparing"):
                executor.begin_question()
                context.reset_reads()
                executor.budget.preserve("answer_finish", 1)
                executor.question_capacity = question_measure(context.snapshot)
                executor.question_capacity.require_current(message)
                agent = build_chat_agent(
                    scope.kb_dir, config["model"], config["language"], bundle, scope=scope
                )
                if focus:
                    from .focus import focus_instruction

                    agent = agent.clone(
                        instructions=(agent.instructions or "")
                        + focus_instruction(scope.kb_dir, focus, scope=scope)
                    )
                selection = resolve_query_views(scope.kb_dir, message, scope=scope)
                agent = restrict_query_agent(agent, selection)
                # Synchronous SDK memory work runs on this actual session owner.
                # Its long model waits cannot block the parent application loop.
                prepared = prepare_stored_conversation(
                    store, conversation, message, executor, agent=agent
                )
                diagnostics = prepared.diagnostics
            context.on_event({"stage": "context_preparation", "context": diagnostics})
            context.on_event({"stage": "answering", "session_id": identity})
            stage = "complete conversation turn"
            stream = iter_agent_response_events(
                agent,
                prepared.sdk_input,
                run_config=build_run_config_from_bundle(config["model"], bundle),
                selection=selection,
                readers_bound=True,
            )
            async with aclosing(stream):
                async for event in stream:
                    if event["event"] == "final":
                        final = event["data"]
                        from ultrakb.agent.chat_trace import complete_trace

                        complete_trace(trace, final["answer"])
                        completed = AnswerResult(
                            "completed",
                            final["answer"],
                            session_id=identity,
                            turn_count=count + 1,
                            changes=(f"saved turn: {identity}",),
                            context=diagnostics,
                            usage=final.get("usage"),
                            answer_outcome=final.get("answer_outcome"),
                            citations=final.get("citations"),
                            image_coverage=executor.image_deliveries.snapshot(),
                            unfinished=tuple(final.get("unfinished", ())),
                        )
                        metadata = {
                            "schema": 1,
                            "execution_id": execution_id,
                            "evidence_publication_id": scope.knowledge.publication_id,
                            "trace": trace,
                            "result": {
                                key: value
                                for key, value in asdict(completed).items()
                                if key != "answer"
                            },
                        }
                        store.append_exchange(
                            identity,
                            uuid4().hex,
                            message,
                            final["answer"],
                            attempt_id=attempt_id,
                            expected_revision=conversation.revision,
                            check_cancelled=context.check_stop,
                            metadata=metadata,
                            execution_id=execution_id,
                        )
                        return completed
                    if event["event"] in {"delta", "tool_call"}:
                        from ultrakb.agent.chat_trace import append_event

                        append_event(
                            trace, event, read_tools=frozenset({"read_file", "get_page_content"})
                        )
                    if event["event"] == "delta":
                        parts.append(event["data"]["text"])
                    context.on_event(event)
                    context.check_stop()
            raise ValueError("The model did not complete a conversation turn")
    except (Exception, asyncio.CancelledError) as error:
        # A lost acknowledgement cannot turn an independently committed exchange
        # into a failed turn or charge another send on the next submission.
        try:
            completed = (
                _committed_result(store, identity, attempt_id, message, execution_id)
                if registered
                else None
            )
        except (OSError, ValueError, sqlite3.Error, RecoveryRequired):
            completed = None
        if completed is not None:
            return completed
        failed = _failed(error, "".join(parts), identity, count, stage, diagnostics)
        if not registered:
            return failed
        try:
            store.finish_submission(execution_id, failed.status, error_code=type(error).__name__)
        except (OSError, ValueError, sqlite3.Error, RecoveryRequired):
            failed = replace(
                failed, diagnostics=(*failed.diagnostics, "submission_confirmation_pending")
            )
        return failed
