"""Defer chat-derived wiki notes until their answer has passed final review."""

import json
from dataclasses import replace

from agents import FunctionTool


class AnswerWrites:
    def __init__(self, kb_dir):
        self.kb_dir = kb_dir.resolve()
        self.pending = {}
        self.results = []

    def guard(self, tool):
        if not isinstance(tool, FunctionTool) or tool.name != "write_file":
            return tool
        original = tool.on_invoke_tool

        async def invoke(context, arguments):
            values = json.loads(arguments)
            target = (self.kb_dir / values["path"]).resolve()
            if target.is_relative_to(self.kb_dir / "wiki/explorations"):
                self.pending[str(target)] = (original, context, values)
                return (
                    "Queued exploration: only the verified final answer will be saved. "
                    "Do not claim it has been written yet."
                )
            return await original(context, arguments)

        return replace(tool, on_invoke_tool=invoke)

    async def publish(self, decision):
        # Keep the original writer's scope, path, cancellation and mutation checks.
        # Rejected answers and cancelled turns never overwrite an existing note.
        if decision.outcome in {"answered", "partial"}:
            for original, context, values in self.pending.values():
                self.results.append(
                    await original(context, json.dumps({**values, "content": decision.answer}))
                )
        self.pending.clear()
