"""Overview provenance and explicit partial artifacts across planning strategies."""

from copy import deepcopy

from openkb.agent.document_window_receipts import window_receipt_id
from openkb.sources import content_id


def current_overview(state):
    snapshot = state.get("overview_snapshot")
    if snapshot:
        return snapshot["text"].strip() + "\n"
    if state.get("overview_parts"):
        texts = [
            state["overview_parts"][key]["text"]
            for key in state.get("overview_tasks", [])
            if key in state["overview_parts"]
        ]
        return "以下为尚未形成全文概览的局部资料。\n\n" + "\n\n".join(texts) + "\n"
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
    if state.get("planning_strategy") == "global-navigation-v2":
        basis = state.get("overview_input", {})
        return {
            **basis,
            "basis": snapshot.get("basis", basis.get("basis", "navigation_summaries")),
            "characters": len(current_overview(state)),
            "partial": not snapshot or bool(snapshot.get("partial")),
            "current_response": snapshot.get("response"),
            "input_clipped": bool(snapshot.get("input_clipped")),
            "original_read_ranges": snapshot.get("original_read_ranges", []),
            "missing_tasks": [
                key
                for key in state.get("overview_tasks", [])
                if state["tasks"][key]["status"] != "accepted"
            ],
            "partial_summaries": len(state.get("overview_parts", {})) if not snapshot else 0,
        }
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


def validate_overview(state, *, total_blocks=None, block_chars=None):
    from openkb.agent.document_range_validation import validate_ranges
    from openkb.sources import valid_id

    if total_blocks is None:
        windows = state.get("windows", [])
        if not isinstance(windows, list) or any(
            not isinstance(row, dict) or type(row.get("target_end")) is not int for row in windows
        ):
            raise ValueError("Invalid overview windows")
        total_blocks = max((row["target_end"] for row in windows), default=0)
    snapshot = state.get("overview_snapshot")
    history = state.get("overview_history", [])
    if snapshot is not None and (
        not isinstance(snapshot, dict) or not isinstance(snapshot.get("text"), str)
    ):
        raise ValueError("Invalid overview snapshot text")
    if not isinstance(history, list):
        raise ValueError("Invalid overview history")
    for record in ([snapshot] if snapshot is not None else []) + history:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("response"), str)
            or type(record.get("input_clipped")) is not bool
            or not isinstance(record.get("text", ""), str)
        ):
            raise ValueError("Invalid overview snapshot")
        valid_id(record["response"])
        if record.get("binding") is not None:
            valid_id(record["binding"])
        if record.get("basis") in {"navigation_summaries", "group_summaries"}:
            planning = state.get("planning_snapshot") or {}
            import json

            from openkb.agent.document_global_context import validate_snapshot

            if (
                not validate_snapshot(planning)
                or record.get("snapshot_id") != planning["id"]
                or record.get("navigation_id")
                != json.loads(planning["context_json"])["navigation_id"]
                or record.get("node_keys") != [row["section_key"] for row in planning["nodes"]]
                or type(record.get("partial")) is not bool
                or "processed" in record
                or not isinstance(record.get("original_read_ranges"), list)
            ):
                raise ValueError("Invalid navigation overview provenance")
            validate_ranges(
                record["original_read_ranges"],
                total_blocks,
                "overview original reads",
                block_chars=block_chars,
            )
            continue
        if not isinstance(record.get("processed"), list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("window"), str)
            or not isinstance(row.get("ranges"), list)
            for row in record["processed"]
        ):
            raise ValueError("Invalid overview processing record")
        for row in record["processed"]:
            validate_ranges(
                row["ranges"], total_blocks, "overview processed", block_chars=block_chars
            )
    legacy = state.get("legacy_overview_fragments", [])
    if not isinstance(legacy, list) or any(
        not isinstance(row, dict)
        or type(row.get("start")) is not int
        or not isinstance(row.get("text"), str)
        for row in legacy
    ):
        raise ValueError("Invalid legacy overview fragments")
