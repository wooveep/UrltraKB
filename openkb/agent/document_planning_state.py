"""Versioned resumable state for independent planning tasks."""

from typing import Any

from openkb.agent import document_windowing
from openkb.agent.document_planning_report import _task_id
from openkb.agent.document_window_receipts import window_receipt_id
from openkb.processing import ProcessingIncomplete, RequestLimits


def _state(
    checkpoints: Any, key: str, windows: list[dict[str, Any]], resume: bool
) -> dict[str, Any]:
    previous = checkpoints.load_recovery(key, "markdown_plan") if resume else None
    if previous is not None and not _valid_state(previous, windows):
        raise ProcessingIncomplete("planning_recovery_invalid", "planning")
    if previous is not None and previous.get("protocol") == "document-planning-acceptance-v3":
        previous.setdefault("filtered", [])
        return previous
    state = {
        "protocol": "document-planning-acceptance-v3",
        "planning_semantics": "tolerant-quality-v1",
        "deferred_suggestions": [],
        "suggestion_annotations": {},
        "batch_notes": [],
        "overview_snapshot": None,
        "overview_history": [],
        "legacy_overview_fragments": [],
        "windows": windows,
        "tasks": {},
        "fragments": {},
        "retained_fragments": [],
        "pages": [],
        "rejected": [],
        "filtered": [],
        "external_references": [],
        "responses": [],
        "request_usage": [],
        "attempts": 0,
    }
    if resume:
        from openkb.agent.document_planning_history import previous_responses

        if historical := previous_responses(checkpoints, windows):
            (
                state["windows"],
                state["tasks"],
                state["replayed_from"],
                state["retained_fragments"],
                state["overview_snapshot"],
                state["overview_history"],
                state["legacy_overview_fragments"],
            ) = historical
    return state


def _valid_state(value: Any, original_windows: list[dict[str, Any]]) -> bool:
    if not isinstance(value, dict) or value.get("protocol") not in {
        "document-planning-acceptance-v2",
        "document-planning-acceptance-v3",
    }:
        return False
    if value["protocol"] == "document-planning-acceptance-v3":
        from openkb.agent.document_planning_semantics import validate_semantics

        try:
            validate_semantics(value)
            from openkb.agent.document_planning_overview import validate_overview

            validate_overview(value)
        except ValueError:
            return False
    mapping_fields = ("tasks", "fragments")
    list_fields = (
        "windows",
        "retained_fragments",
        "pages",
        "rejected",
        "external_references",
        "responses",
        "request_usage",
    )
    if any(not isinstance(value.get(name), dict) for name in mapping_fields) or any(
        not isinstance(value.get(name), list) for name in list_fields
    ):
        return False
    if not isinstance(value.get("filtered", []), list):
        return False
    if type(value.get("attempts")) is not int or value["attempts"] < 0:
        return False
    if type(value.get("recovery_requests", 0)) is not int or value.get("recovery_requests", 0) < 0:
        return False
    if (
        original_windows
        and not value["windows"]
        or any(
            not isinstance(window, dict)
            or type(window.get("target_start")) is not int
            or type(window.get("target_end")) is not int
            or not any(
                parent["target_start"]
                <= window["target_start"]
                < window["target_end"]
                <= parent["target_end"]
                for parent in original_windows
            )
            for window in value["windows"]
        )
    ):
        return False

    def deltas(windows: list[dict[str, Any]]) -> dict[int, int]:
        edges: dict[int, int] = {}
        for window in windows:
            start, end = window["target_start"], window["target_end"]
            edges[start] = edges.get(start, 0) + 1
            edges[end] = edges.get(end, 0) - 1
        return {position: count for position, count in edges.items() if count}

    if deltas(value["windows"]) != deltas(original_windows):
        return False
    if any(
        not isinstance(key, str) or not isinstance(text, str)
        for key, text in value["fragments"].items()
    ):
        return False
    if any(
        not isinstance(row, dict)
        or set(row) != {"start", "text"}
        or type(row["start"]) is not int
        or not isinstance(row["text"], str)
        for row in value["retained_fragments"]
    ):
        return False
    if any(not isinstance(row, dict) for name in list_fields[2:] for row in value[name]):
        return False
    return all(
        isinstance(key, str)
        and isinstance(task, dict)
        and task.get("status") in {"pending", "accepted", "partial", "skipped", "retired"}
        and type(task.get("attempts")) is int
        and task["attempts"] >= 0
        and isinstance(task.get("replay", []), list)
        and all(
            isinstance(row, dict)
            and isinstance(row.get("content"), str)
            and isinstance(row.get("finish_reason"), str)
            for row in task.get("replay", [])
        )
        and (task.get("reason") is None or isinstance(task["reason"], str))
        and (
            task.get("raw") is None
            or (
                isinstance(task["raw"], dict)
                and isinstance(task["raw"].get("content"), str)
                and isinstance(task["raw"].get("finish_reason"), str)
            )
        )
        for key, task in value["tasks"].items()
    )


def _split_window(
    state: dict[str, Any], index: int, source: Any, parsed: Any, limits: RequestLimits
) -> bool:
    """Admit new child tasks without replaying accepted parent components."""
    window = state["windows"][index]
    children = document_windowing.reload_planning_target(source, parsed, window)
    if not children:
        return False
    parent_id = window_receipt_id(window)
    fragment = state["fragments"].pop(parent_id, None)
    if fragment:
        state.setdefault("retained_fragments", []).append(
            {"start": window["target_start"], "text": fragment}
        )
    for subtask in ("overview", "pages"):
        parent_task = state["tasks"].get(_task_id(window, subtask), {})
        for child in children:
            if parent_task.get("status") == "accepted" and _covers_input(
                parent_task, child, parsed
            ):
                state["tasks"][_task_id(child, subtask)] = {
                    "status": parent_task["status"],
                    "attempts": 0,
                    "raw": None,
                    "reason": parent_task.get("reason"),
                    "no_pages_recommended": parent_task.get("no_pages_recommended", False),
                    "inherited_from": _task_id(window, subtask),
                    "input_ranges": parent_task["input_ranges"],
                }
        if parent_task:
            parent_task.update(
                status="retired", superseded_by=[_task_id(child, subtask) for child in children]
            )
    state["windows"][index : index + 1] = children
    return True


def _covers_input(task, child, parsed):
    from openkb.agent.document_planning_projection import _range_intervals
    from openkb.agent.document_planning_report import _target_ranges

    provided = _range_intervals(parsed, task.get("input_ranges", []))
    needed = _range_intervals(parsed, _target_ranges(child, parsed))
    return bool(needed) and all(
        any(i == index and left <= start and end <= right for i, left, right in provided)
        for index, start, end in needed
    )


def _persist(checkpoints: Any, key: str, state: dict[str, Any]) -> None:
    checkpoints.save_recovery(key, "markdown_plan", state)


def _raw_value(raw: Any) -> dict[str, str]:
    return {
        "content": str(raw or ""),
        "finish_reason": str(getattr(raw, "finish_reason", "stop") or "stop"),
    }
