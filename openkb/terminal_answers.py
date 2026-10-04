"""Terminal presentation of the same answer events used by the desktop and HTTP."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from openkb.agent.answer_text import visible_answer

if TYPE_CHECKING:
    from rich.live import Live


class AnswerRenderer:
    """Render progress only; callers use the application result as the saved answer."""

    def __init__(self, *, enabled: bool = True, use_color: bool = False, raw: bool = False):
        self.enabled, self.use_color, self.raw = enabled, use_color, raw
        self.parts: list[str] = []
        self.live: Live | None = None
        self.last_text = False
        self.visible = ""
        self.completed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._flush()

    def _flush(self):
        if self.live:
            from openkb.agent.chat import _make_markdown

            self.live.update(_make_markdown(self.visible))
            self.live.stop()
            self.live = None
        elif self.last_text:
            sys.stdout.write("\n")
        if self.visible:
            self.completed.append(self.visible)
        self.parts = []
        self.visible = ""
        self.last_text = False

    def finish(self, answer: str) -> None:
        """Some providers send the final answer only in their terminal result."""
        answer = visible_answer(answer)
        shown = "\n".join([*self.completed, self.visible])
        if self.enabled and answer and not shown.rstrip().endswith(answer):
            if not answer.startswith(self.visible):
                self._flush()
            self._render(answer)

    def _render(self, text: str) -> None:
        addition = text[len(self.visible) :]
        self.visible = text
        self.last_text = bool(text)
        if not text:
            return
        if self.use_color and not self.raw:
            from rich.live import Live

            from openkb.agent.chat import _make_markdown, _make_rich_console

            if self.live is None:
                self.live = Live(console=_make_rich_console(), vertical_overflow="visible")
                self.live.start()
            self.live.update(_make_markdown(text))
        else:
            sys.stdout.write(addition)
            sys.stdout.flush()

    def __call__(self, event: dict):
        if not self.enabled:
            return
        kind, data = event.get("event"), event.get("data", {})
        if kind == "answer_start":
            self._flush()
        elif kind == "delta":
            self.parts.append(data["text"])
            self._render(visible_answer("".join(self.parts), streaming=True))
        elif kind == "tool_call":
            from openkb.agent.chat import _build_style, _fmt, _format_tool_line

            self._flush()
            _fmt(
                _build_style(self.use_color),
                (
                    "class:tool",
                    _format_tool_line(data.get("name", "?"), data.get("arguments", "")) + "\n",
                ),
            )
