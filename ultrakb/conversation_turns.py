"""Stable committed exchanges; display arrays are projections of these records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

SESSION_FORMAT_VERSION = 2


def exchange_digest(user_text: str, assistant_text: str) -> str:
    return hashlib.sha256(
        json.dumps([user_text, assistant_text], ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ConversationTurn:
    turn_id: str
    order: int
    user_text: str
    assistant_text: str
    content_digest: str
    attempt_id: str
    trace: list[dict[str, Any]] = field(default_factory=list)
    answer_outcome: str | None = None
    usage: dict[str, int | None] | None = None
    citations: dict | None = None

    def __post_init__(self):
        if any(
            not isinstance(text, str) or not text.strip()
            for text in (self.user_text, self.assistant_text)
        ):
            raise ValueError("A committed exchange requires non-empty original text")

        from ultrakb.application.answer_citations import validate_citation_snapshot

        validate_citation_snapshot(self.citations)
        if self.usage is not None:
            from ultrakb.agent.token_usage import TokenUsage

            TokenUsage.from_dict(self.usage)

    @classmethod
    def create(
        cls,
        order,
        user_text,
        assistant_text,
        attempt_id=None,
        trace=None,
        outcome=None,
        usage=None,
        citations=None,
    ):
        if not isinstance(user_text, str) or not isinstance(assistant_text, str):
            raise ValueError("A committed exchange requires original text")
        return cls(
            uuid4().hex,
            order,
            user_text,
            assistant_text,
            exchange_digest(user_text, assistant_text),
            attempt_id or uuid4().hex,
            trace or [],
            outcome,
            dict(usage) if usage is not None else None,
            citations,
        )


def load_turns(value: Any) -> list[ConversationTurn]:
    if not isinstance(value, list):
        raise ValueError("Invalid committed conversation turns")
    turns, identities, attempts = [], set(), set()
    for order, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValueError("Invalid committed conversation turn")
        try:
            turn = ConversationTurn(**raw)
        except TypeError as error:
            raise ValueError("Invalid committed conversation turn") from error
        if (
            type(turn.order) is not int
            or turn.order != order
            or any(
                not isinstance(s, str)
                for s in (
                    turn.turn_id,
                    turn.user_text,
                    turn.assistant_text,
                    turn.content_digest,
                    turn.attempt_id,
                )
            )
            or not turn.turn_id
            or not turn.attempt_id
            or turn.turn_id in identities
            or turn.attempt_id in attempts
            or turn.content_digest != exchange_digest(turn.user_text, turn.assistant_text)
            or not isinstance(turn.trace, list)
            or any(not isinstance(step, dict) for step in turn.trace)
            or (turn.answer_outcome is not None and not isinstance(turn.answer_outcome, str))
        ):
            raise ValueError("Invalid committed conversation identity, order or content digest")
        turns.append(turn)
        identities.add(turn.turn_id)
        attempts.add(turn.attempt_id)
    return turns
