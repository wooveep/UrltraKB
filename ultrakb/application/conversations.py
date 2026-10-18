"""Complete question and conversation operations for local adapters.

The underlying agents and persisted session format remain shared with the
CLI and REST. Temporary deltas are separate from a committed complete turn.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import aclosing, nullcontext
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ultrakb.agent.chat_session import ChatSession, load_session
from ultrakb.application.answers import save_exploration
from ultrakb.application.execution import ExecutionContext
from ultrakb.config import resolve_effective_config
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.llm_usage_execution import track_import_usage
from ultrakb.locks import LockCancelled, async_kb_lock, async_session_lock
from ultrakb.log import append_log
from ultrakb.model_outputs import ModelOutputs
from ultrakb.mutation import RecoveryRequired

if TYPE_CHECKING:
    from ultrakb.storage.wiki_projection import ProjectionResult


@dataclass(frozen=True)
class AnswerResult:
    status: Literal["completed", "stopped", "failed", "blocked"]
    answer: str
    saved_path: str | None = None
    session_id: str | None = None
    turn_count: int = 0
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    error: str | None = None
    unfinished: tuple[str, ...] = ()
    usage: dict[str, int | None] | None = None
    model_usage: dict | None = None
    context: dict | None = None
    answer_outcome: (
        Literal["answered", "partial", "insufficient_evidence", "evidence_rejected"] | None
    ) = None
    image_coverage: tuple[dict, ...] = ()
    result_publication_id: str | None = None
    projection: ProjectionResult | None = None
    candidate_revision_id: str | None = None
    reason_code: str | None = None
    citations: dict | None = None
    diagnostics: tuple[str, ...] = ()


def _with_image_receipt(operation):
    @wraps(operation)
    async def run(*args, **kwargs):
        scope = kwargs.get("scope")
        identity = scope.knowledge.identity if scope and scope.knowledge else None
        context = kwargs.get("context") or ExecutionContext(storage_identity=identity)
        if context.storage_identity != identity:
            raise ValueError("Question execution and knowledge must use the same storage identity")
        before = context.executor.image_deliveries if context.executor else None
        previous_execution = context.storage_execution
        try:
            result = await operation(*args, **{**kwargs, "context": context})
        finally:
            if context.executor:
                context.executor.budget.release("answer_finish")
        after = context.executor.image_deliveries if context.executor else None
        if (
            identity is not None
            and context.storage_execution is not None
            and (context.storage_execution is not previous_execution or context._storage_running)
        ):
            from ultrakb.agent.token_usage import aggregate_token_usage
            from ultrakb.storage.usage_store import UsageStore

            try:
                receipt = UsageStore(scope.kb_dir, identity).receipt(
                    execution_id=context.storage_execution.execution_id
                )
            except (OSError, sqlite3.Error):
                result = replace(
                    result,
                    reason_code=result.reason_code or "usage_unavailable",
                    error=result.error or "Usage details could not be loaded",
                    unfinished=(*result.unfinished, "load usage details"),
                    diagnostics=(*result.diagnostics, "usage_unavailable"),
                )
            else:
                result = replace(
                    result,
                    model_usage=receipt,
                    usage=aggregate_token_usage(receipt["current"])
                    if receipt["current"]["requests"]
                    else result.usage,
                )
        return replace(
            result,
            image_coverage=(after.snapshot() if after is not None and after is not before else ())
            or result.image_coverage,
        )

    return run


def _validate_question(kb_dir: Path, question: str, *, scope: KnowledgeScope | None = None) -> Path:
    scope = resolve_scope(kb_dir, scope)
    root = kb_dir.expanduser().resolve()
    if not (root / ".ultrakb/config.yaml").is_file() or (
        scope.knowledge is None and not scope.wiki_dir.is_dir()
    ):
        raise FileNotFoundError(f"Knowledge base not found: {root}")
    if not question.strip():
        raise ValueError("Enter a question before starting a conversation")
    return root


def _settle_usage(session: ChatSession, previous, fallback=None):
    from ultrakb.agent.token_usage import add_usage, execution_usage

    usage = execution_usage(fallback)
    if usage is not None:
        session.token_usage = add_usage(previous, usage)
        session.save()
    return usage


@track_import_usage
@_with_image_receipt
async def ask_question(
    kb_dir: Path,
    question: str,
    *,
    save: bool = False,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
) -> AnswerResult:
    query_scope = scope
    scope = resolve_scope(kb_dir, scope)
    root = _validate_question(kb_dir, question, scope=scope)
    context = context or ExecutionContext()
    parts: list[str] = []
    path = None
    answer = ""
    result: AnswerResult | None = None
    unfinished_stage = "prepare context"
    from ultrakb.model_tokenizer import ContextCapacityError

    try:
        async with (
            nullcontext()
            if scope.knowledge is not None
            else async_kb_lock(
                root / ".ultrakb",
                exclusive=True,
                cancelled=context.cancelled,
                on_wait=context.waiting,
            )
        ):
            with context.begin(root) as bundle:
                with context.activity("preparing") as preparation:
                    assert context.executor is not None
                    context.executor.begin_question()
                    context.reset_reads()
                    from ultrakb.agent.query import (
                        build_query_agent,
                        build_run_config_from_bundle,
                        iter_agent_response_events,
                    )

                    assert context.snapshot is not None
                    config = context.snapshot.values()["effective"]
                    from ultrakb.application.query_views import resolve_query_views

                    selection = resolve_query_views(root, question, scope=query_scope)
                    model = config["model"]
                    schema_options = {}
                    if scope.knowledge is not None:
                        from ultrakb.schema import AGENTS_MD

                        schema_options["schema_md"] = (
                            scope.knowledge.read("AGENTS.md").decode("utf-8")
                            if "AGENTS.md" in scope.reading_view.files
                            else AGENTS_MD
                        )
                    agent = build_query_agent(
                        str(scope.wiki_dir), model, config["language"], bundle, **schema_options
                    )
                    from ultrakb.model_tokenizer import ContextCapacityError, question_measure

                    try:
                        context.executor.question_capacity = question_measure(context.snapshot)
                        context.executor.question_capacity.require_current(question)
                    except ContextCapacityError as error:
                        preparation["status"] = "failed"
                        context._storage_result = "failed"
                        return AnswerResult(
                            "blocked", "", error=str(error), unfinished=("prepare context",)
                        )
                    from ultrakb.agent.query_evidence import restrict_query_agent

                    agent = restrict_query_agent(agent, selection)
                context.on_event({"stage": "answering"})
                unfinished_stage = "answer question"
                stream = iter_agent_response_events(
                    agent,
                    question,
                    run_config=build_run_config_from_bundle(model, bundle),
                    selection=selection,
                    readers_bound=True,
                )
                async with aclosing(stream):
                    async for event in stream:
                        if event["event"] == "final":
                            context.check_stop()
                            answer = event["data"]["answer"]
                            unfinished_stage = "save answer"
                            if scope.knowledge is not None:
                                from ultrakb.context.workers import run_blocking

                                from .storage_questions import finish_storage_question

                                def finish():
                                    nonlocal result
                                    result = finish_storage_question(
                                        scope,
                                        question,
                                        event["data"],
                                        save=save,
                                        check_cancelled=context.check_stop,
                                    )

                                with context.activity("saving"):
                                    try:
                                        await run_blocking(finish)
                                    except asyncio.CancelledError:
                                        # run_blocking has waited for the actual worker.
                                        # A committed result still belongs to this request.
                                        if result is None:
                                            raise
                                assert result is not None
                                if result.status != "completed":
                                    context._storage_result = "failed"
                                return result
                            from ultrakb.published_knowledge import knowledge_operation

                            with (
                                context.activity("saving"),
                                knowledge_operation(root, operation="save-query-result"),
                            ):
                                path = (
                                    save_exploration(root, question, answer, scope=scope)
                                    if save
                                    else None
                                )
                                unfinished_stage = "record question log"
                                append_log(scope.wiki_dir, "query", question, scope=scope)
                            return AnswerResult(
                                "completed",
                                answer,
                                str(path) if path else None,
                                usage=event["data"].get("usage"),
                                answer_outcome=event["data"].get("answer_outcome"),
                                unfinished=tuple(event["data"].get("unfinished", ())),
                                resources=(str(path),) if path else (),
                                changes=(f"created: {path.relative_to(root).as_posix()}",)
                                if path
                                else (),
                            )
                        if context.cancelled():
                            break
                        if event["event"] == "delta":
                            parts.append(event["data"]["text"])
                        context.on_event(event)
                from ultrakb.agent.token_usage import execution_usage

                context._storage_result = "cancelled"
                return AnswerResult(
                    "stopped",
                    "".join(parts),
                    unfinished=(unfinished_stage,),
                    usage=execution_usage(),
                )

    except (Exception, asyncio.CancelledError) as exc:
        from ultrakb.agent.token_usage import execution_usage

        if result is not None:
            return replace(
                result,
                reason_code=result.reason_code or "execution_confirmation_pending",
                error=result.error
                or "The result is retained; execution cleanup needs confirmation",
                unfinished=(*result.unfinished, "confirm execution cleanup"),
                diagnostics=(*result.diagnostics, "execution_confirmation_pending"),
            )
        stopped = isinstance(exc, (LockCancelled, asyncio.CancelledError))
        context._storage_result = "cancelled" if stopped else "failed"
        return AnswerResult(
            "blocked"
            if isinstance(exc, (RecoveryRequired, ContextCapacityError))
            else "stopped"
            if stopped
            else "failed",
            answer or "".join(parts),
            str(path) if path else None,
            resources=(str(path),) if path else (),
            changes=(f"created: {path.relative_to(root).as_posix()}",) if path else (),
            error=str(exc)
            if isinstance(exc, ContextCapacityError)
            else f"Question did not complete ({type(exc).__name__})",
            unfinished=(unfinished_stage,),
            usage=execution_usage(),
        )


@track_import_usage
@_with_image_receipt
async def continue_conversation(
    kb_dir: Path,
    message: str,
    *,
    session_id: str | None = None,
    new_session_id: str | None = None,
    attempt_id: str | None = None,
    submission_order: int | None = None,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
    focus: tuple = (),
) -> AnswerResult:
    query_scope = scope
    scope = resolve_scope(kb_dir, scope)
    root = _validate_question(kb_dir, message, scope=scope)
    context = context or ExecutionContext()
    if scope.knowledge is not None:
        from .storage_conversations import continue_stored_conversation

        return await continue_stored_conversation(
            scope,
            message,
            session_id=session_id,
            new_session_id=new_session_id,
            attempt_id=attempt_id,
            submission_order=submission_order,
            context=context,
            focus=focus,
        )
    session = ChatSession.new(root, "", "", identity=new_session_id, view_id=scope.view_id)
    identity = session_id or session.id
    async with async_session_lock(
        root, identity, cancelled=context.cancelled, on_wait=context.waiting
    ):
        async with async_kb_lock(
            root / ".ultrakb", exclusive=True, cancelled=context.cancelled, on_wait=context.waiting
        ):
            if session_id:
                # Reload after both locks: a queued continuation cannot revive
                # a deleted session or build on a stale completed history.
                session = load_session(root, session_id)
            elif new_session_id and session.path.exists():
                session = load_session(root, new_session_id)
            session.require_view(scope.view_id)
            with context.begin(root) as bundle:
                with context.activity("preparing") as preparation:
                    assert context.executor is not None
                    executor = context.executor
                    executor.begin_question()
                    context.reset_reads()
                    previous_usage = session.token_usage
                    from ultrakb.agent.chat import build_chat_session_agent, iter_chat_turn_events
                    from ultrakb.agent.query import build_run_config_from_bundle

                    if not session.model:
                        config = (await asyncio.to_thread(resolve_effective_config, root))[0]
                        session.model, session.language = config["model"], config["language"]
                        if session._version is not None:
                            # A recovered queued submission already exists on disk.
                            # Bind its first execution before the iterator reloads it.
                            session.save()
                    attempt_id = session.begin_attempt(
                        message, identity=attempt_id, submission_order=submission_order
                    )
                    from ultrakb.model_tokenizer import ContextCapacityError, question_measure

                    try:
                        # Index catch-up and recall share this task's sends with
                        # the answer loop. Protect its final send before either.
                        executor.budget.preserve("answer_finish", 1)
                        executor.question_capacity = question_measure(context.snapshot)
                        executor.question_capacity.require_current(message)
                    except Exception as error:
                        preparation["status"] = "failed"
                        usage = _settle_usage(session, previous_usage)
                        return AnswerResult(
                            "blocked"
                            if isinstance(error, (ContextCapacityError, RecoveryRequired))
                            else "stopped"
                            if isinstance(error, LockCancelled)
                            else "failed",
                            "",
                            session_id=session.id,
                            turn_count=session.turn_count,
                            resources=(str(session.path),),
                            error=str(error)
                            if isinstance(error, ContextCapacityError)
                            else f"Context preparation did not complete ({type(error).__name__})",
                            unfinished=("prepare context",),
                            usage=usage,
                        )
                    agent = await asyncio.to_thread(
                        build_chat_session_agent, root, session, bundle, scope=scope
                    )
                    if focus:
                        from ultrakb.application.focus import focus_instruction

                        # Read the focus inventory on the thread owning the KB
                        # lease; a different thread cannot borrow that lease.
                        instruction = focus_instruction(root, focus, scope=scope)
                        agent = agent.clone(instructions=(agent.instructions or "") + instruction)
                    from ultrakb.agent.query_evidence import restrict_query_agent
                    from ultrakb.application.query_views import resolve_query_views
                    from ultrakb.context.preparation import prepare_conversation_input
                    from ultrakb.context.workers import run_blocking

                    selection = resolve_query_views(root, message, scope=query_scope)
                    agent = restrict_query_agent(agent, selection)
                    try:
                        prepared = await run_blocking(
                            lambda: prepare_conversation_input(
                                session, message, executor, agent=agent, selection=selection
                            )
                        )
                    except asyncio.CancelledError:
                        _settle_usage(session, previous_usage)
                        raise
                    except Exception as error:
                        preparation["status"] = "failed"
                        usage = _settle_usage(session, previous_usage)
                        return AnswerResult(
                            "blocked"
                            if isinstance(error, (ContextCapacityError, RecoveryRequired))
                            else "stopped"
                            if isinstance(error, LockCancelled)
                            else "failed",
                            "",
                            session_id=session.id,
                            turn_count=session.turn_count,
                            resources=(str(session.path),),
                            error=str(error)
                            if isinstance(error, ContextCapacityError)
                            else f"Context preparation did not complete ({type(error).__name__})",
                            unfinished=("prepare context",),
                            usage=usage,
                        )
                context.on_event({"stage": "context_preparation", "context": prepared.diagnostics})
                context.on_event({"stage": "answering", "session_id": session.id})
                stream = iter_chat_turn_events(
                    agent,
                    session,
                    message,
                    run_config=build_run_config_from_bundle(session.model, bundle),
                    outputs=(outputs := ModelOutputs()),
                    attempt_id=attempt_id,
                    scope=query_scope,
                    selection=selection,
                    prepared_input=prepared,
                )
                parts = []
                usage = None
                try:
                    async with aclosing(stream):
                        async for event in stream:
                            if event["event"] == "final":
                                context.check_stop()
                                return AnswerResult(
                                    "completed",
                                    event["data"]["answer"],
                                    session_id=session.id,
                                    turn_count=session.turn_count,
                                    resources=(*outputs.resources, str(session.path)),
                                    changes=(*outputs.changes, f"saved turn: {session.id}"),
                                    usage=event["data"].get("usage", usage),
                                    context=prepared.diagnostics,
                                    answer_outcome=event["data"].get("answer_outcome"),
                                    unfinished=tuple(event["data"].get("unfinished", ())),
                                )
                            if context.cancelled():
                                break
                            if event["event"] == "delta":
                                parts.append(event["data"]["text"])
                            if event["event"] == "usage":
                                usage = event["data"]
                            context.on_event(event)
                except asyncio.CancelledError:
                    _settle_usage(session, previous_usage, usage)
                    raise
                except Exception as exc:
                    usage = _settle_usage(session, previous_usage, usage)
                    return AnswerResult(
                        "blocked"
                        if isinstance(exc, (RecoveryRequired, ContextCapacityError))
                        else "stopped"
                        if isinstance(exc, LockCancelled)
                        else "failed",
                        "".join(parts),
                        session_id=session.id,
                        turn_count=session.turn_count,
                        resources=(*outputs.resources, str(session.path)),
                        changes=outputs.changes,
                        error=str(exc)
                        if isinstance(exc, ContextCapacityError)
                        else f"Conversation did not complete ({type(exc).__name__})",
                        unfinished=("complete conversation turn",),
                        usage=usage,
                        context=prepared.diagnostics,
                    )
                usage = _settle_usage(session, previous_usage, usage)
                return AnswerResult(
                    "stopped",
                    "".join(parts),
                    session_id=session.id,
                    turn_count=session.turn_count,
                    resources=(*outputs.resources, str(session.path)),
                    changes=outputs.changes,
                    unfinished=("complete conversation turn",),
                    usage=usage,
                    context=prepared.diagnostics,
                )


@dataclass(frozen=True)
class ConversationView:
    id: str
    title: str
    model: str
    language: str
    turns: tuple[tuple[str, str], ...]
    version: str
    incomplete: tuple[tuple[int, str], ...] = ()
    usage: dict[str, int | None] | None = None
    answer_outcomes: tuple[str | None, ...] = ()
    answers: tuple = ()

    @property
    def timeline_answers(self):
        rows = []
        for index in range(len(self.turns) + 1):
            rows.extend(None for after, _ in self.incomplete if after == index)
            if index < len(self.turns):
                rows.append(self.answers[index] if index < len(self.answers) else None)
        return tuple(rows)

    @property
    def timeline(self) -> tuple[tuple[str, str], ...]:
        """Display unfinished submissions in order, separate from reusable model turns."""
        rows: list[tuple[str, str]] = []
        for index in range(len(self.turns) + 1):
            rows.extend(
                (message, "这次回答未完成，可以继续提问。")
                for after, message in self.incomplete
                if after == index
            )
            if index < len(self.turns):
                rows.append(self.turns[index])
        return tuple(rows)


def read_conversation(
    kb_dir: Path, session_id: str, *, view_id: str | None = None
) -> ConversationView:
    from ultrakb.application.sessions import load_conversation

    session = load_conversation(kb_dir, session_id, view_id=view_id)
    from ultrakb.application.answer_records import completed_answers

    return ConversationView(
        session.id,
        session.title,
        session.model,
        session.language,
        tuple(zip(session.user_turns, session.assistant_texts)),
        session._version or "",
        tuple((item["after_turn"], item["message"]) for item in session.incomplete),
        session.token_usage,
        tuple(session.answer_outcomes),
        completed_answers(session),
    )
