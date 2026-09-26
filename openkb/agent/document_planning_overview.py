"""Cumulative overview snapshots and explicit partial fallback history."""

from copy import deepcopy

from openkb.agent.document_window_receipts import window_receipt_id
from openkb.sources import content_id


def current_overview(state):
    snapshot = state.get("overview_snapshot")
    if snapshot:
        return snapshot["text"].strip() + "\n"
    from openkb.agent.document_planning_report import _overview_text, ordered_fragments

    fragments = ordered_fragments(state)
    if not fragments:
        return ""
    return "以下为尚未形成全文概览的局部资料。\n\n" + _overview_text(fragments)


def accept_snapshot(state, window, ranges, raw, accepted):
    """A complete reply replaces the current text; a partial reply never does."""
    wid = window_receipt_id(window)
    if accepted.reason:
        state["fragments"][wid] = accepted.text
        return
    previous = state.get("overview_snapshot") or {}
    processed = deepcopy(previous.get("processed", []))
    if not any(row["window"] == wid for row in processed):
        processed.append({"window": wid, "ranges": ranges})
    snapshot = {
        "text": accepted.text,
        "response": content_id(raw["content"]),
        "binding": raw.get("binding"),
        "processed": processed,
        "input_clipped": raw.get("projection", {}).get("overview_input_clipped", False),
    }
    state["overview_snapshot"] = snapshot
    state.setdefault("overview_history", []).append(
        {key: value for key, value in snapshot.items() if key != "text"}
    )


def overview_summary(state):
    snapshot = state.get("overview_snapshot") or {}
    missing = [
        window_receipt_id(window)
        for window in state["windows"]
        if state["tasks"].get(window_receipt_id(window) + ":overview", {}).get("status")
        != "accepted"
    ]
    return {
        "characters": len(current_overview(state)),
        "partial": not snapshot or bool(missing),
        "missing_windows": missing,
        "input_clipped": any(row.get("input_clipped") for row in state.get("overview_history", [])),
        "current_response": snapshot.get("response"),
        "legacy_fragments": len(state.get("legacy_overview_fragments", [])),
    }


def validate_overview(state):
    snapshot = state.get("overview_snapshot")
    if snapshot is not None:
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("text"), str)
            or not isinstance(snapshot.get("response"), str)
            or type(snapshot.get("input_clipped")) is not bool
        ):
            raise ValueError("Invalid overview snapshot")
        if not isinstance(snapshot.get("processed"), list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("window"), str)
            or not isinstance(row.get("ranges"), list)
            for row in snapshot["processed"]
        ):
            raise ValueError("Invalid overview processing record")
    if not isinstance(state.get("overview_history", []), list):
        raise ValueError("Invalid overview history")
