"""A completed exchange owns its Markdown and reported usage, never a UI transcript."""

from dataclasses import dataclass, field

from ultrakb.agent.answer_text import visible_answer


@dataclass(frozen=True)
class CompletedAnswer:
    identity: str
    question: str
    markdown: str
    usage: dict[str, int | None] | None = None
    trace: tuple[dict, ...] = field(default=(), repr=False)
    outcome: str | None = None
    citations: dict | None = None
    kb_dir: str = ""
    view_id: str = "legacy"


def completed_answers(session):
    return tuple(
        CompletedAnswer(
            turn.turn_id,
            turn.user_text,
            visible_answer(turn.assistant_text),
            dict(turn.usage) if turn.usage is not None else None,
            tuple(turn.trace),
            turn.answer_outcome,
            turn.citations,
            str(session.path.parent.parent.parent),
            session.view_id,
        )
        for turn in session.turns
    )
