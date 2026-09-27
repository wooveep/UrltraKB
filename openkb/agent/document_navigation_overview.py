"""Bounded overview tasks from frozen navigation, with disjoint summary fallback."""

from __future__ import annotations

import json

from openkb.agent.document_global_context import empty_evidence, fits, messages_for, split_topics
from openkb.agent.document_planning_bindings import capture_request
from openkb.agent.document_planning_overview import current_overview
from openkb.agent.document_planning_report import _path
from openkb.agent.document_planning_response import accept_overview
from openkb.agent.document_planning_state import _persist, _raw_value
from openkb.agent.document_protocol import plan_messages
from openkb.execution_measurement import (
    record_first_inspectable,
    request_marker,
    request_usage_since,
)
from openkb.locks import atomic_write_text
from openkb.processing import (
    InputTooLarge,
    OutputTruncated,
    ProcessingIncomplete,
    processing_checkpoint,
)
from openkb.sources import content_id

BUDGET_REASONS = {
    "request_budget_exhausted",
    "token_budget_exhausted",
    "time_budget_exhausted",
    "pages_request_reserved",
    "pages_tokens_reserved",
    "planning_recovery_budget",
}


def plan_navigation_overview(
    state,
    checkpoints,
    key,
    source,
    parsed,
    settings,
    limits,
    entity_types,
    schema,
    conditions,
    budget,
    *,
    bundle=None,
    mock_caller=None,
):
    from openkb.agent.document_markdown_planner import _call, _Response

    snapshot = state["planning_snapshot"]
    context = json.loads(snapshot["context_json"])
    nodes = snapshot["nodes"]
    if overview := state.get("overview_snapshot"):
        pending = any(
            state["tasks"][key]["status"] == "pending" for key in state.get("overview_tasks", [])
        )
        if not overview.get("partial") or not pending:
            return
        state["overview_snapshot"] = None
    state.setdefault("overview_tasks", [])
    state.setdefault("overview_parts", {})
    state["overview_input"] = {
        "basis": "navigation_summaries",
        "snapshot_id": snapshot["id"],
        "navigation_id": context["navigation_id"],
        **context["summary_input"],
        "projection": context["projection"],
        "original_read_ranges": [],
    }

    def messages(rows, kind, parts=(), recovery=""):
        return plan_messages(
            empty_evidence(source, parsed),
            {"partial_summaries": list(parts)},
            {
                "kind": kind,
                "sections": [row["section_key"] for row in rows],
                "summary_basis": "derived_navigation",
                "snapshot_id": snapshot["id"],
            },
            rows,
            "",
            entity_types,
            schema,
            settings.get("language", ""),
            source_conditions=conditions,
            subtask="overview",
            recovery=recovery,
            planning_context=context,
        )

    # Reserve a real, minimally projected pages request after any overview call.
    pages_messages = messages_for(
        snapshot,
        [{**row, "summary": ""} for row in nodes[:1]],
        state,
        source,
        parsed,
        settings,
        limits,
        entity_types,
        schema,
        conditions,
        detail=False,
        shown=0,
        overview_limit=0,
    )
    try:
        pages_cost = budget.cost(pages_messages)
    except InputTooLarge:
        pages_cost = limits.input_capacity + limits.output_tokens

    def persist():
        _persist(checkpoints, key, state)
        text = current_overview(state)
        if text:
            atomic_write_text(_path(checkpoints, "overview", key, ".md"), text)
            record_first_inspectable()

    def run(rows, kind, parts=(), *, clipped=False, complete=False):
        task_key = "navigation-overview:" + content_id(
            {"snapshot": snapshot["id"], "kind": kind, "rows": rows, "parts": parts}
        )
        task = state["tasks"].setdefault(
            task_key,
            {
                "component": "overview",
                "kind": kind,
                "snapshot_id": snapshot["id"],
                "sections": [row["section_key"] for row in rows],
                "status": "pending",
                "attempts": 0,
                "raw": None,
                "reason": None,
                "input_clipped": clipped,
            },
        )
        if task_key not in state["overview_tasks"]:
            state["overview_tasks"].append(task_key)
        if task["status"] in {"accepted", "skipped"}:
            return state["overview_parts"].get(task_key)
        while task["attempts"] < limits.max_attempts or task.get("raw") is not None:
            processing_checkpoint("planning")
            request = messages(rows, kind, parts, task.get("reason") or "")
            if not fits(request, settings, limits):
                task.update(status="skipped", reason="planning_context_capacity")
                break
            try:
                if task.get("raw") is None:
                    binding = capture_request(
                        request, source, parsed, {"task": task_key}, checkpoints.store
                    )
                    budget.admit(request, reserve_pages=pages_cost)
                    task["attempts"] += 1
                    state["attempts"] += 1
                    if task["attempts"] > 1:
                        state["recovery_requests"] = state.get("recovery_requests", 0) + 1
                    persist()
                    marker = request_marker()
                    try:
                        from openkb.processing_reservation import reserve_later_work

                        with reserve_later_work(
                            requests=1,
                            tokens=pages_cost,
                            attempts=limits.max_attempts - task["attempts"] + 1,
                        ):
                            response = _call(
                                request, settings, limits, bundle, mock_caller, "overview"
                            )
                    finally:
                        state["request_usage"].extend(
                            {**row, "planning_component": "overview", "task": task_key}
                            for row in request_usage_since(marker)
                        )
                        extra = max(0, request_marker() - marker - 1)
                        task["attempts"] += extra
                        state["attempts"] += extra
                        state["recovery_requests"] = state.get("recovery_requests", 0) + extra
                    task["raw"] = {**_raw_value(response), "binding": binding["id"]}
                    state["responses"].append(
                        {"task": task_key, "attempt": task["attempts"], **task["raw"]}
                    )
                    persist()
                raw = task["raw"]
                accepted = accept_overview(_Response(raw["content"], raw["finish_reason"]))
                if parts and len(accepted.text) >= sum(len(row["text"]) for row in parts):
                    # A merge must shrink the next level, even when the model ignores
                    # its concise-summary instructions. Keep the source parts intact.
                    accepted.reason = "overview_merge_not_reduced"
                if accepted.text:
                    part = {
                        "text": accepted.text,
                        "response": content_id(raw["content"]),
                        "binding": raw.get("binding"),
                        "task": task_key,
                        "sections": task["sections"],
                        "partial": bool(accepted.reason),
                        "input_clipped": clipped,
                    }
                    state["overview_parts"][task_key] = part
                    if complete and not accepted.reason:
                        state["overview_snapshot"] = {
                            **part,
                            "basis": "group_summaries" if parts else "navigation_summaries",
                            "snapshot_id": snapshot["id"],
                            "navigation_id": context["navigation_id"],
                            "node_keys": [row["section_key"] for row in nodes],
                            "original_read_ranges": [],
                            "partial": clipped or any(row.get("partial") for row in parts),
                        }
                        state["overview_history"].append(
                            {k: v for k, v in state["overview_snapshot"].items() if k != "text"}
                        )
                task.update(
                    raw=None,
                    reason=accepted.reason,
                    status="accepted" if accepted.text and not accepted.reason else "partial",
                )
                persist()
                if task["status"] == "accepted":
                    return state["overview_parts"].get(task_key)
            except (InputTooLarge, OutputTruncated) as exc:
                task["reason"] = (
                    "overview_truncated"
                    if isinstance(exc, OutputTruncated)
                    else "planning_context_capacity"
                )
            except ProcessingIncomplete as exc:
                if exc.reason not in BUDGET_REASONS:
                    persist()
                    raise
                state["budget_limited"] = exc.reason
                task.update(status="skipped", reason=exc.reason)
                persist()
                return None
        task.update(status="skipped", reason=task.get("reason") or "overview_unusable")
        persist()
        return state["overview_parts"].get(task_key)

    if not nodes:
        run([], "navigation_overview")
        return
    if not context["projection"].get("overview_requires_groups"):
        run([], "navigation_overview", complete=True)
        return

    # Input grouping is disjoint. A failed task's retry never multiplies into
    # fresh child attempts, and every dispatch still consumes the total budget.
    queue = split_topics(nodes) or [nodes]
    parts, incomplete = [], False
    while queue:
        rows = queue.pop(0)
        if not fits(messages(rows, "topic_summary"), settings, limits):
            children = split_topics(rows)
            if children:
                queue[:0] = children
                continue
            original = rows[0]["summary"]
            rows = [dict(rows[0])]
            while rows[0]["summary"] and not fits(
                messages(rows, "topic_summary"), settings, limits
            ):
                rows[0]["summary"] = rows[0]["summary"][: len(rows[0]["summary"]) // 2]
            clipped = rows[0]["summary"] != original
        else:
            clipped = False
        part = run(rows, "topic_summary", clipped=clipped)
        if part:
            parts.append(part)
        incomplete |= part is None or bool(part and (part["partial"] or part["input_clipped"]))
        if state.get("budget_limited"):
            return
    while parts:
        if fits(messages([], "overview_merge", parts), settings, limits):
            run([], "overview_merge", parts, clipped=incomplete, complete=True)
            return
        packs, pending = [], []
        for part in parts:
            if pending and not fits(
                messages([], "overview_merge", pending + [part]), settings, limits
            ):
                packs.append(pending)
                pending = []
            pending.append(part)
        if pending:
            packs.append(pending)
        reduced = []
        for pack in packs:
            if not fits(messages([], "overview_merge", pack), settings, limits):
                # A single oversized saved fragment cannot bypass admission.
                # Preserve it as a partial artifact; do not invent a full overview.
                return
            part = run([], "overview_merge", pack)
            if part is None or part["partial"]:
                return
            reduced.append(part)
            if state.get("budget_limited"):
                return
        if sum(len(row["text"]) for row in reduced) >= sum(len(row["text"]) for row in parts):
            return
        parts = reduced
