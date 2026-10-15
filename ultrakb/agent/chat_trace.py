"""Fold delivered chat events into the display trace retained with a turn."""

from __future__ import annotations

READ_TOOLS = frozenset({"read_file", "get_page_content"})


def append_event(trace: list[dict], event: dict, *, read_tools=READ_TOOLS) -> None:
    """Record text and permitted read calls in arrival order."""
    kind, data = event["event"], event["data"]
    if kind == "delta":
        text = data.get("text", "")
        if text:
            if trace and trace[-1].get("kind") == "text":
                trace[-1]["text"] += text
            else:
                trace.append({"kind": "text", "text": text})
    elif kind == "tool_call" and data.get("name") in read_tools:
        trace.append({"kind": "tool", "name": data.get("name"), "arguments": data.get("arguments")})


def complete_trace(trace: list[dict], answer: str) -> None:
    """Retain an unstreamed answer without duplicating substantive deltas."""
    if answer and not any(
        item.get("kind") == "text" and item.get("text", "").strip() for item in trace
    ):
        trace.append({"kind": "text", "text": answer})
