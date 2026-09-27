"""Sequential, bounded page selection from frozen navigation and saved overview."""

from __future__ import annotations

import json

from openkb.agent.document_global_context import (
    fits,
    freeze_context,
    messages_for,
    split_topics,
    task_record,
)
from openkb.agent.document_plan import PagePlan
from openkb.agent.document_planning_bindings import capture_request, load_binding
from openkb.agent.document_planning_candidates import record_candidate_attempt
from openkb.agent.document_planning_response import accept_pages
from openkb.agent.document_planning_semantics import record_semantics
from openkb.agent.document_planning_state import _persist, _raw_value
from openkb.agent.document_window_schedule import no_readable_body
from openkb.execution_measurement import (
    record_first_inspectable,
    request_marker,
    request_usage_since,
)
from openkb.processing import (
    InputTooLarge,
    OutputTruncated,
    ProcessingIncomplete,
    processing_checkpoint,
)


def plan_global_pages(
    state,
    checkpoints,
    key,
    source,
    parsed,
    navigation,
    catalog,
    settings,
    limits,
    entity_types,
    schema,
    conditions,
    existing_targets,
    budget,
    *,
    bundle=None,
    mock_caller=None,
):
    from openkb.agent.document_markdown_planner import _call, _Response

    snapshot = freeze_context(
        state,
        navigation,
        source,
        parsed,
        catalog,
        settings,
        limits,
        entity_types,
        schema,
        conditions,
    )
    nodes = snapshot["nodes"]
    if "planning_tasks" not in state:
        task_key, task = task_record(snapshot, nodes)
        state["planning_tasks"] = [task_key]
        state["tasks"][task_key] = task
    state["planning_mode"] = "global" if len(state["planning_tasks"]) == 1 else "topic_groups"
    _persist(checkpoints, key, state)
    index = 0
    while index < len(state["planning_tasks"]):
        task_key = state["planning_tasks"][index]
        task = state["tasks"][task_key]
        if task["status"] in {"accepted", "skipped", "retired"}:
            index += 1
            continue
        rows = task["sections"]
        family = task["family"]

        def used():
            return sum(
                row["attempts"] for row in state["tasks"].values() if row.get("family") == family
            )

        while used() < limits.max_attempts or task.get("raw") is not None:
            processing_checkpoint("planning")
            if no_readable_body(parsed) or (not nodes and not state.get("overview_snapshot")):
                task.update(status="skipped", reason="planning_context_unavailable")
                break
            overview_limit = None
            messages = messages_for(
                snapshot,
                rows,
                state,
                source,
                parsed,
                settings,
                limits,
                entity_types,
                schema,
                conditions,
                recovery=task.get("reason") or "",
            )
            if not fits(messages, settings, limits):
                messages = messages_for(
                    snapshot,
                    rows,
                    state,
                    source,
                    parsed,
                    settings,
                    limits,
                    entity_types,
                    schema,
                    conditions,
                    recovery=task.get("reason") or "",
                    detail=False,
                )
            if not fits(messages, settings, limits) and _split(
                state, task_key, snapshot, rows, index, after_dispatch=bool(used())
            ):
                break
            shown = len(state["pages"]) + len(state.get("deferred_suggestions", []))
            while not fits(messages, settings, limits) and shown:
                shown //= 2
                messages = messages_for(
                    snapshot,
                    rows,
                    state,
                    source,
                    parsed,
                    settings,
                    limits,
                    entity_types,
                    schema,
                    conditions,
                    recovery=task.get("reason") or "",
                    detail=False,
                    shown=shown,
                )
            from openkb.agent.document_planning_overview import current_overview

            overview_limit = len(current_overview(state))
            while not fits(messages, settings, limits) and overview_limit > 300:
                overview_limit //= 2
                messages = messages_for(
                    snapshot,
                    rows,
                    state,
                    source,
                    parsed,
                    settings,
                    limits,
                    entity_types,
                    schema,
                    conditions,
                    recovery=task.get("reason") or "",
                    detail=False,
                    shown=shown,
                    overview_limit=overview_limit,
                )
            if not fits(messages, settings, limits):
                if _split(state, task_key, snapshot, rows, index, after_dispatch=bool(used())):
                    break
                compact_rows = [{**row, "summary": ""} for row in rows]
                messages = messages_for(
                    snapshot,
                    compact_rows,
                    state,
                    source,
                    parsed,
                    settings,
                    limits,
                    entity_types,
                    schema,
                    conditions,
                    recovery=task.get("reason") or "",
                    detail=False,
                    shown=shown,
                    overview_limit=overview_limit,
                )
                task["projection"] = {"summaries_omitted": len(rows), "suggestions_shown": shown}
                if fits(messages, settings, limits):
                    pass
                else:
                    task.update(status="skipped", reason="planning_context_capacity")
                    break
            carry = json.loads(messages[-1]["content"])["carry"]
            task["input_projection"] = {
                **carry["suggestions"],
                "overview_input_clipped": carry["overview"]["input_clipped"],
            }
            if task.get("raw") is None:
                binding = capture_request(
                    messages, source, parsed, {"task": task_key}, checkpoints.store
                )
                try:
                    budget.admit(messages)
                    task["attempts"] += 1
                    state["attempts"] += 1
                    if used() > 1:
                        state["recovery_requests"] = state.get("recovery_requests", 0) + 1
                    _persist(checkpoints, key, state)
                    marker = request_marker()
                    try:
                        from openkb.processing_reservation import reserve_later_work

                        with reserve_later_work(attempts=limits.max_attempts - used() + 1):
                            response = _call(
                                messages, settings, limits, bundle, mock_caller, "pages"
                            )
                    finally:
                        usage = request_usage_since(marker)
                        state["request_usage"].extend(
                            {**row, "planning_component": "pages", "task": task_key}
                            for row in usage
                        )
                        extra = max(0, request_marker() - marker - 1)
                        task["attempts"] += extra
                        state["attempts"] += extra
                        state["recovery_requests"] = state.get("recovery_requests", 0) + extra
                    task["raw"] = {
                        **_raw_value(response),
                        "binding": binding["id"],
                        "snapshot_id": snapshot["id"],
                    }
                    state["responses"].append(
                        {"task": task_key, "attempt": task["attempts"], **task["raw"]}
                    )
                    _persist(checkpoints, key, state)
                except (InputTooLarge, OutputTruncated) as exc:
                    task["reason"] = (
                        "pages_truncated"
                        if isinstance(exc, OutputTruncated)
                        else "planning_context_capacity"
                    )
                    if _split(state, task_key, snapshot, rows, index, after_dispatch=True):
                        break
                    continue
                except ProcessingIncomplete as exc:
                    if exc.reason == "planning_recovery_budget":
                        task.update(status="skipped", reason=exc.reason)
                        break
                    if exc.reason not in {
                        "request_budget_exhausted",
                        "token_budget_exhausted",
                        "time_budget_exhausted",
                    }:
                        raise
                    task.update(status="skipped", reason=exc.reason)
                    state["budget_limited"] = exc.reason
                    break
            raw = task["raw"]
            accepted = [PagePlan.from_dict(item) for item in state["pages"]]
            result = accept_pages(
                _Response(raw["content"], raw["finish_reason"]),
                navigation=nodes,
                target=[],
                parsed=parsed,
                entity_types=entity_types,
                existing_targets=existing_targets,
                allowed_update_targets={
                    row["target"] for row in json.loads(snapshot["context_json"])["catalog"]
                },
                catalog_titles={path: title for path, title, _ in snapshot["catalog"]},
                catalog_types={
                    row["target"]: row["type"]
                    for row in json.loads(snapshot["context_json"])["catalog"]
                    if "type" in row
                },
                accepted=accepted,
                default_entity_type=settings.get("default_entity_type"),
                source_identity=source.source_id,
                request_binding=load_binding(checkpoints.store, raw["binding"], source, parsed),
                deferred=state["deferred_suggestions"],
                annotations=state["suggestion_annotations"],
            )
            state["pages"] = [page.to_dict() for page in accepted + result.pages]
            record_candidate_attempt(state, task_key, task["attempts"], result)
            record_semantics(state, result)
            task.update(
                raw=None,
                no_pages_recommended=result.no_pages,
                status="accepted" if result.usable and not result.truncated else "partial",
                reason="pages_truncated"
                if result.truncated
                else None
                if result.usable
                else "pages_unparseable",
            )
            _persist(checkpoints, key, state)
            if result.pages or result.deferred_suggestions:
                record_first_inspectable()
            if task["status"] == "accepted":
                break
            if result.truncated and _split(
                state, task_key, snapshot, rows, index, after_dispatch=True
            ):
                break
        if task["status"] not in {"accepted", "retired", "skipped"}:
            task.update(status="skipped", reason=task.get("reason") or "planning_recovery_budget")
        _persist(checkpoints, key, state)
        if task["status"] != "retired":
            index += 1
    active = [state["tasks"][key] for key in state["planning_tasks"]]
    state["planning_mode"] = (
        "topic_groups" if any(row["kind"] == "topic_group" for row in active) else "global"
    )


def _split(state, task_key, snapshot, rows, index, *, after_dispatch):
    groups = split_topics(rows)
    if not groups:
        return False
    task = state["tasks"][task_key]
    children = []
    for group in groups:
        child_key, child = task_record(
            snapshot, group, family=task["family"] if after_dispatch else None
        )
        state["tasks"][child_key] = child
        children.append(child_key)
    state["planning_mode"] = "topic_groups"
    task.update(status="retired", superseded_by=children)
    state["planning_tasks"][index : index + 1] = children
    return True
