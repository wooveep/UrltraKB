"""Prepare accepted pages once, with bounded recovery for missing subject locations."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from openkb.agent.document_page_resolution import PreparedPage, preparation_rules, prepare_page
from openkb.agent.document_page_sources import accept_locations, source_messages_for
from openkb.agent.document_planning_runtime import SOURCE_REQUEST_LIMIT, preparation_max_chars
from openkb.agent.document_preparation_allowance import (
    LocationAllowance,
    LocationAllowanceExhausted,
)
from openkb.agent.document_recovery import remember_preparation, suggestion_identity
from openkb.execution_allowance import request_allowance
from openkb.execution_measurement import request_marker, request_usage_since
from openkb.processing import (
    InputTooLarge,
    OutputTruncated,
    RequestLimits,
    budget_settlement_scope,
    processing_scope,
    request_admission_scope,
)


@dataclass
class PreparationContext:
    source: Any
    parsed: Any
    navigation: Any
    reader: Any
    settings: dict
    checkpoints: Any
    bundle: Any = None
    retry_skipped: bool = False
    mock_caller: Any = None


def _validate_state(state, snapshot, page_keys):
    if (
        not isinstance(state, dict)
        or state.get("protocol") != "page-preparation-v1"
        or type(state.get("round")) is not int
        or state["round"] < 1
        or state.get("status") not in {"running", "settled"}
        or not isinstance(state.get("pages"), dict)
        or not isinstance(state.get("attempts"), list)
        or not isinstance(state.get("usage"), list)
    ):
        raise ValueError("Invalid page preparation recovery")
    for index, row in enumerate(state["attempts"]):
        if (
            not isinstance(row, dict)
            or type(row.get("ordinal")) is not int
            or row.get("ordinal") != index + 1
            or type(row.get("round")) is not int
            or not 1 <= row["round"] <= state["round"]
            or not isinstance(row.get("page"), str)
            or row["page"] not in state["pages"]
            or row.get("status") not in {"reserved", "dispatched", "response_received"}
        ):
            raise ValueError("Invalid saved page-source attempt")
    nodes = (snapshot or {}).get("nodes", [])
    for key, value in state["pages"].items():
        if (
            key not in page_keys
            or not isinstance(value, dict)
            or not isinstance(value.get("identity"), str)
        ):
            raise ValueError("Invalid saved page-source identity")
        for field in ("raw", "accepted_response", "reason"):
            if value.get(field) is not None and not isinstance(value[field], str):
                raise ValueError("Invalid saved page-source response")
        if "finished_round" in value and (
            type(value["finished_round"]) is not int
            or not 1 <= value["finished_round"] <= state["round"]
        ):
            raise ValueError("Invalid saved page-source round")
        if "response_truncated" in value and type(value["response_truncated"]) is not bool:
            raise ValueError("Invalid saved response status")
        if not isinstance(value.get("details", []), list) or any(
            row not in nodes for row in value.get("details", [])
        ):
            raise ValueError("Invalid saved navigation details")
        if not isinstance(value.get("accepted_clues", []), list) or any(
            not isinstance(clue, (str, dict, list)) for clue in value.get("accepted_clues", [])
        ):
            raise ValueError("Invalid saved location clues")
        if not isinstance(value.get("responses", []), list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("content"), str)
            or not isinstance(row.get("unresolved"), list)
            for row in value.get("responses", [])
        ):
            raise ValueError("Invalid saved source responses")
    for row in state["usage"]:
        if not isinstance(row, dict) or row.get("page") not in state["pages"]:
            raise ValueError("Invalid saved location usage")
        if not isinstance(row.get("stage"), str):
            raise ValueError("Invalid saved usage stage")
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_miss_tokens",
        ):
            if row.get(key) is not None and (type(row[key]) is not int or row[key] < 0):
                raise ValueError("Invalid saved token usage")
        if (
            row.get("transport_complete") is not None
            and type(row["transport_complete"]) is not bool
        ):
            raise ValueError("Invalid saved transport status")
        seconds = row.get("request_seconds")
        if seconds is not None and (
            type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
        ):
            raise ValueError("Invalid saved request duration")


def _retain_locations(selection, snapshot, saved):
    """Keep resolved leaves while requesting only undisplayed children of chosen parents."""
    context = json.loads(snapshot["context_json"])
    shown = {row.get("section_key") for row in [*context["topics"], *saved.get("details", [])]}
    accepted = saved.setdefault("accepted_clues", [])
    details = []
    for entry in selection.entries:
        children = [row for row in snapshot["nodes"] if row["parent"] in entry["keys"]]
        hidden = [row for row in children if row["section_key"] not in shown]
        if hidden:
            details.extend(row for row in children if row not in details)
        elif entry["clue"] not in accepted:
            accepted.append(entry["clue"])
    return details


def _prepare_retained(page, context, snapshot, state, saved, limits, save, reason=None):
    if not saved.get("accepted_clues"):
        return None, reason
    raw = json.dumps({"Subject": saved["accepted_clues"]}, ensure_ascii=False)
    selection = accept_locations(raw, snapshot["nodes"], context.parsed)
    candidate = deepcopy(page)
    candidate.subject_ranges = deepcopy(selection.ranges)
    candidate.location_hints.extend(
        {"role": "subject", "value": clue}
        for clue in selection.clues
        if {"role": "subject", "value": clue} not in candidate.location_hints
    )
    unresolved = [clue for row in saved.get("responses", []) for clue in row["unresolved"]]
    if unresolved:
        candidate.planning_notes.append(
            "定位恢复未解释线索：" + json.dumps(unresolved, ensure_ascii=False)
        )
    if reason:
        candidate.planning_notes.append("部分定位未完成：" + reason)
    candidate.state = "pending_evidence"
    prepared = prepare_page(
        candidate,
        context.source,
        context.parsed,
        context.navigation,
        context.reader,
        max_chars=preparation_max_chars(limits),
        retry_skipped=True,
    )
    saved.update(finished_round=state["round"], reason=prepared.reason, accepted_response=raw)
    save()
    return prepared, prepared.reason


def _locate(page, context, snapshot, state, save, limits, identity):
    from openkb.agent.document_markdown_planner import _call

    source, parsed = context.source, context.parsed
    saved = state["pages"].setdefault(page.key, {"identity": identity})
    if saved["identity"] != identity:
        raise ValueError("Page-source recovery suggestion changed")
    reason = saved.get("reason", "no_subject_evidence")
    details = saved.get("details", [])
    allowance = LocationAllowance(state, page.key, limits, save)
    if saved.get("accepted_response") is not None:
        saved["raw"] = saved["accepted_response"]
    elif saved.get("finished_round") == state["round"]:
        return None, reason
    while allowance.used(page=True) < limits.max_attempts or saved.get("raw") is not None:
        if saved.get("raw") is None:
            if allowance.used() >= SOURCE_REQUEST_LIMIT:
                return _prepare_retained(
                    page,
                    context,
                    snapshot,
                    state,
                    saved,
                    limits,
                    save,
                    "page_sources_document_limit",
                )
            try:
                messages = source_messages_for(
                    page,
                    snapshot,
                    source,
                    parsed,
                    context.settings,
                    limits,
                    reason=reason,
                    details=details,
                )
            except InputTooLarge:
                return _prepare_retained(
                    page,
                    context,
                    snapshot,
                    state,
                    saved,
                    limits,
                    save,
                    "page_sources_input_capacity",
                )
            marker = request_marker()
            try:
                with request_allowance(allowance), request_admission_scope(allowance.admission):
                    if context.mock_caller is not None:
                        # A test seam has no transport, so record its single simulated attempt.
                        allowance.before_reserve({}, 0)
                    raw = _call(
                        messages,
                        context.settings,
                        limits,
                        context.bundle,
                        context.mock_caller,
                        "page_sources",
                    )
                saved["raw"] = str(raw)  # Section keys/titles are not wire identity aliases.
                saved["response_truncated"] = getattr(raw, "finish_reason", None) == "length"
                if allowance.request:
                    allowance.request["status"] = "response_received"
                save()  # A restart can accept this response without paying again.
            except LocationAllowanceExhausted:
                return _prepare_retained(
                    page,
                    context,
                    snapshot,
                    state,
                    saved,
                    limits,
                    save,
                    "page_sources_attempt_limit",
                )
            except InputTooLarge:
                return _prepare_retained(
                    page,
                    context,
                    snapshot,
                    state,
                    saved,
                    limits,
                    save,
                    "page_sources_input_capacity",
                )
            except OutputTruncated:
                reason = "page_sources_truncated"
                saved["reason"] = reason
                save()
                continue
            finally:
                state["usage"].extend(
                    {**row, "page": page.key} for row in request_usage_since(marker)
                )
                with budget_settlement_scope():
                    save()
        raw = saved.pop("raw")
        selection = accept_locations(raw, snapshot["nodes"], parsed)
        saved.setdefault("responses", []).append(
            {"content": raw, "unresolved": selection.unresolved}
        )
        if selection.ranges:
            new_children = _retain_locations(selection, snapshot, saved)
            if new_children:
                details = new_children
                reason = "Select supporting sections for the same page from these child summaries."
                saved.update(reason=reason, details=details)
                save()
                continue
            return _prepare_retained(page, context, snapshot, state, saved, limits, save)
        if selection.empty:
            if saved.get("accepted_clues"):
                return _prepare_retained(
                    page,
                    context,
                    snapshot,
                    state,
                    saved,
                    limits,
                    save,
                    "remaining_sections_unsupported",
                )
            saved.update(finished_round=state["round"], reason="page_sources_no_support")
            save()
            return None, "page_sources_no_support"
        reason = (
            "No supplied chapter could be resolved. Use an existing title or section key; "
            "return None if unsupported."
        )
        saved.update(reason=reason, details=details)
        save()
    return _prepare_retained(
        page, context, snapshot, state, saved, limits, save, "page_sources_attempt_limit"
    )


def prepare_planned_pages(plan, context: PreparationContext) -> list[PreparedPage]:
    """The production and online-test seam; mutates only preparation state in plan."""
    from openkb.agent.document_plan import to_dict
    from openkb.agent.document_planning_report import refresh_execution_report

    limits = RequestLimits.from_config(context.settings)
    maximum = preparation_max_chars(limits)
    snapshot = plan.metadata.get("planning_snapshot")
    key = context.checkpoints.identity(
        "page-preparation-v1",
        {
            "plan": plan.metadata["recovery_key"],
            "rules": preparation_rules(),
            "max_chars": maximum,
            "page_attempts": limits.max_attempts,
            "document_attempts": SOURCE_REQUEST_LIMIT,
        },
    )
    state = context.checkpoints.load_recovery(key, "page_preparation")
    if state is None:
        state = {
            "protocol": "page-preparation-v1",
            "round": 1,
            "status": "running",
            "pages": {},
            "attempts": [],
            "usage": [],
        }
    else:
        _validate_state(state, snapshot, {page.key for page in plan.pages})
        if context.retry_skipped:
            state["round"] += 1
    state["status"] = "running"

    def save():
        context.checkpoints.save_recovery(key, "page_preparation", state)

    def save_plan():
        plan.metadata["page_preparation"] = {
            "recovery_key": key,
            "round": state["round"],
            "status": state["status"],
            "document_request_limit": SOURCE_REQUEST_LIMIT,
            "attempts": deepcopy(state["attempts"]),
            "usage": deepcopy(state["usage"]),
            "pages": {
                name: {
                    k: v
                    for k, v in row.items()
                    if k
                    not in {"raw", "accepted_response", "responses", "details", "accepted_clues"}
                }
                for name, row in state["pages"].items()
            },
        }
        context.checkpoints.save_recovery(plan.metadata["recovery_key"], "plan", to_dict(plan))

    save()
    results = []
    with processing_scope(context.settings):
        for index, page in enumerate(plan.pages):
            if page.state == "pending_evidence":
                remember_preparation(plan, page)
            prepared = prepare_page(
                page,
                context.source,
                context.parsed,
                context.navigation,
                context.reader,
                max_chars=maximum,
                retry_skipped=context.retry_skipped,
            )
            if prepared.reason == "no_subject_evidence" and snapshot:
                identity = plan.metadata.get("prepared_suggestions", {}).get(
                    page.key, suggestion_identity(page)
                )
                located, reason = _locate(page, context, snapshot, state, save, limits, identity)
                if located is not None:
                    prepared = located
                else:
                    prepared.page.planning_notes.append("定位恢复跳过：" + str(reason))
                    prepared = PreparedPage(prepared.page, reason=reason)
            plan.pages[index] = prepared.page
            results.append(prepared)
            save()
            save_plan()
        state["status"] = "settled"
        save()
        save_plan()
        refresh_execution_report(context.checkpoints, plan)
    return results
