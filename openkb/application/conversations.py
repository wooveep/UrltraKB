"""Complete question and conversation operations for local adapters.

The underlying agents and persisted session format remain shared with the
CLI and REST. Temporary deltas are separate from a committed complete turn.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from openkb.agent.chat_session import ChatSession, load_session
from openkb.agent.request_budget import visual_task_budget
from openkb.application.answers import save_exploration
from openkb.application.execution import ExecutionContext
from openkb.compilation_report import collect_compile_report
from openkb.config import resolve_effective_config
from openkb.locks import async_kb_lock, async_session_lock
from openkb.log import append_log
from openkb.model_outputs import ModelOutputs, model_output_scope
from openkb.mutation import RecoveryRequired
from openkb.processing import ProcessingIncomplete


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
    usage: dict = field(default_factory=dict)


def _validate_question(kb_dir: Path, question: str) -> Path:
    root = kb_dir.expanduser().resolve()
    if not (root / ".openkb/config.yaml").is_file() or not (root / "wiki").is_dir():
        raise FileNotFoundError(f"Knowledge base not found: {root}")
    if not question.strip():
        raise ValueError("Enter a question before starting a conversation")
    return root


def _failure_code(error):
    if isinstance(error, ProcessingIncomplete):
        return getattr(error, "diagnostic_code", error.reason)
    return type(error).__name__


async def ask_question(
    kb_dir: Path,
    question: str,
    *,
    save: bool = False,
    context: ExecutionContext | None = None,
) -> AnswerResult:
    root = _validate_question(kb_dir, question)
    context = context or ExecutionContext()
    async with async_kb_lock(
        root / ".openkb", exclusive=True, cancelled=context.cancelled, on_wait=context.waiting
    ):
        with (
            context.begin(root) as bundle,
            model_output_scope(root),
            collect_compile_report() as report,
            visual_task_budget(root),
        ):
            from openkb.agent.query import (
                build_query_agent,
                build_run_config_from_bundle,
                iter_agent_response_events,
            )

            config = (await asyncio.to_thread(resolve_effective_config, root))[0]
            model = config["model"]
            agent = build_query_agent(str(root / "wiki"), model, config["language"], bundle)
            context.on_event({"stage": "answering"})
            stream = iter_agent_response_events(
                agent, question, run_config=build_run_config_from_bundle(model, bundle)
            )
            parts = []
            path = None
            answer = ""
            unfinished_stage = "answer question"
            try:
                async with aclosing(stream):
                    async for event in stream:
                        if event["event"] == "final":
                            answer = event["data"]["answer"]
                            unfinished_stage = "save answer"
                            path = save_exploration(root, question, answer) if save else None
                            unfinished_stage = "record question log"
                            append_log(root / "wiki", "query", question)
                            return AnswerResult(
                                "completed",
                                answer,
                                str(path) if path else None,
                                resources=(str(path),) if path else (),
                                usage=report.usage,
                                changes=(f"created: {path.relative_to(root).as_posix()}",)
                                if path
                                else (),
                            )
                        if context.cancelled():
                            break
                        if event["event"] == "delta":
                            parts.append(event["data"]["text"])
                        context.on_event(event)
            except (Exception, ProcessingIncomplete) as exc:
                return AnswerResult(
                    "blocked" if isinstance(exc, RecoveryRequired) else "failed",
                    answer or "".join(parts),
                    str(path) if path else None,
                    resources=(str(path),) if path else (),
                    changes=(f"created: {path.relative_to(root).as_posix()}",) if path else (),
                    error=f"Question did not complete ({_failure_code(exc)})",
                    unfinished=(unfinished_stage,),
                    usage=report.usage,
                )
            return AnswerResult("stopped", "".join(parts), usage=report.usage)


async def continue_conversation(
    kb_dir: Path,
    message: str,
    *,
    session_id: str | None = None,
    new_session_id: str | None = None,
    attempt_id: str | None = None,
    submission_order: int | None = None,
    context: ExecutionContext | None = None,
) -> AnswerResult:
    root = _validate_question(kb_dir, message)
    context = context or ExecutionContext()
    session = ChatSession.new(root, "", "", identity=new_session_id)
    identity = session_id or session.id
    async with async_session_lock(
        root, identity, cancelled=context.cancelled, on_wait=context.waiting
    ):
        async with async_kb_lock(
            root / ".openkb", exclusive=True, cancelled=context.cancelled, on_wait=context.waiting
        ):
            if session_id:
                # Reload after both locks: a queued continuation cannot revive
                # a deleted session or build on a stale completed history.
                session = load_session(root, session_id)
            elif new_session_id and session.path.exists():
                session = load_session(root, new_session_id)
            with (
                context.begin(root) as bundle,
                collect_compile_report() as report,
                visual_task_budget(root),
            ):
                from openkb.agent.chat import build_chat_session_agent, iter_chat_turn_events
                from openkb.agent.query import build_run_config_from_bundle

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
                # Snapshot source evidence on the execution lease owner. SDK tool
                # workers receive detached readers and cannot reacquire this lock.
                agent = build_chat_session_agent(root, session, bundle)
                context.on_event({"stage": "answering", "session_id": session.id})
                stream = iter_chat_turn_events(
                    agent,
                    session,
                    message,
                    run_config=build_run_config_from_bundle(session.model, bundle),
                    outputs=(outputs := ModelOutputs()),
                    attempt_id=attempt_id,
                )
                parts = []
                try:
                    async with aclosing(stream):
                        async for event in stream:
                            if event["event"] == "final":
                                return AnswerResult(
                                    "completed",
                                    event["data"]["answer"],
                                    session_id=session.id,
                                    usage=report.usage,
                                    turn_count=session.turn_count,
                                    resources=(*outputs.resources, str(session.path)),
                                    changes=(*outputs.changes, f"saved turn: {session.id}"),
                                )
                            if context.cancelled():
                                break
                            if event["event"] == "delta":
                                parts.append(event["data"]["text"])
                            context.on_event(event)
                except (Exception, ProcessingIncomplete) as exc:
                    return AnswerResult(
                        "blocked" if isinstance(exc, RecoveryRequired) else "failed",
                        "".join(parts),
                        session_id=session.id,
                        usage=report.usage,
                        turn_count=session.turn_count,
                        resources=(*outputs.resources, str(session.path)),
                        changes=outputs.changes,
                        error=f"Conversation did not complete ({_failure_code(exc)})",
                        unfinished=("complete conversation turn",),
                    )
                return AnswerResult(
                    "stopped",
                    "".join(parts),
                    session_id=session.id,
                    usage=report.usage,
                    turn_count=session.turn_count,
                    resources=(*outputs.resources, str(session.path)),
                    changes=outputs.changes,
                    unfinished=("complete conversation turn",),
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


def read_conversation(kb_dir: Path, session_id: str) -> ConversationView:
    from openkb.locks import kb_read_lock

    with kb_read_lock(kb_dir / ".openkb"):
        session = load_session(kb_dir, session_id)
        return ConversationView(
            session.id,
            session.title,
            session.model,
            session.language,
            tuple(zip(session.user_turns, session.assistant_texts)),
            session._version or "",
            tuple((item["after_turn"], item["message"]) for item in session.incomplete),
        )
