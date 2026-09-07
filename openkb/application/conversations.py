"""Complete question and conversation operations for local adapters.

The underlying agents and persisted session format remain shared with the
CLI and REST. Temporary deltas are separate from a committed complete turn.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.agent.chat_session import ChatSession, load_session
from openkb.application.answers import save_exploration
from openkb.application.execution import ExecutionContext
from openkb.config import resolve_effective_config
from openkb.locks import async_kb_lock, async_session_lock
from openkb.log import append_log


@dataclass(frozen=True)
class AnswerResult:
    status: Literal["completed", "stopped"]
    answer: str
    saved_path: str | None = None
    session_id: str | None = None
    turn_count: int = 0


def _validate_question(kb_dir: Path, question: str) -> Path:
    root = kb_dir.expanduser().resolve()
    if not (root / ".openkb/config.yaml").is_file() or not (root / "wiki").is_dir():
        raise FileNotFoundError(f"Knowledge base not found: {root}")
    if not question.strip():
        raise ValueError("Enter a question before starting a conversation")
    return root


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
        with context.begin(root) as bundle:
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
            async with aclosing(stream):
                async for event in stream:
                    if event["event"] == "final":
                        answer = event["data"]["answer"]
                        path = save_exploration(root, question, answer) if save else None
                        append_log(root / "wiki", "query", question)
                        return AnswerResult("completed", answer, str(path) if path else None)
                    if context.cancelled():
                        break
                    if event["event"] == "delta":
                        parts.append(event["data"]["text"])
                    context.on_event(event)
            return AnswerResult("stopped", "".join(parts))


async def continue_conversation(
    kb_dir: Path,
    message: str,
    *,
    session_id: str | None = None,
    context: ExecutionContext | None = None,
) -> AnswerResult:
    root = _validate_question(kb_dir, message)
    context = context or ExecutionContext()
    session = ChatSession.new(root, "", "")
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
            with context.begin(root) as bundle:
                from openkb.agent.chat import build_chat_session_agent, iter_chat_turn_events
                from openkb.agent.query import build_run_config_from_bundle

                if not session_id:
                    config = (await asyncio.to_thread(resolve_effective_config, root))[0]
                    session.model, session.language = config["model"], config["language"]
                agent = await asyncio.to_thread(build_chat_session_agent, root, session, bundle)
                context.on_event({"stage": "answering", "session_id": session.id})
                stream = iter_chat_turn_events(
                    agent,
                    session,
                    message,
                    run_config=build_run_config_from_bundle(session.model, bundle),
                )
                parts = []
                async with aclosing(stream):
                    async for event in stream:
                        if event["event"] == "final":
                            return AnswerResult(
                                "completed",
                                event["data"]["answer"],
                                session_id=session.id,
                                turn_count=session.turn_count,
                            )
                        if context.cancelled():
                            break
                        if event["event"] == "delta":
                            parts.append(event["data"]["text"])
                        context.on_event(event)
                return AnswerResult(
                    "stopped", "".join(parts), session_id=session.id, turn_count=session.turn_count
                )


@dataclass(frozen=True)
class ConversationView:
    id: str
    title: str
    model: str
    language: str
    turns: tuple[tuple[str, str], ...]
    version: str


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
        )
