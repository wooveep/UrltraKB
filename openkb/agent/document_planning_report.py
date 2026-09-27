"""Accepted Markdown planning artifacts and honest source-linked reports."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from openkb.agent import document_planning_support
from openkb.agent.document_plan import DocumentPlan, OverviewPlan, PagePlan, validate_plan
from openkb.agent.document_plan_annotations import ExternalReference, PlanningOmission
from openkb.agent.document_planning_candidates import rejected_candidate_summary
from openkb.agent.document_planning_result import PlanningResult
from openkb.agent.document_window_receipts import window_receipt_id
from openkb.execution_measurement import record_document_totals
from openkb.locks import atomic_write_text
from openkb.planning_coverage import planning_coverage, planning_page_scopes
from openkb.sources import content_id, valid_id


def usage_totals(state: dict[str, Any]) -> dict[str, Any]:
    rows = state.get("request_usage", [])

    def total(field: str) -> int | None:
        values = [row.get(field) for row in rows]
        return sum(values) if values and all(type(value) is int for value in values) else None

    return {
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "cache_write_tokens": total("cache_write_tokens"),
        "cache_miss_tokens": total("cache_miss_tokens"),
        "requests": len(rows),
        "request_seconds": sum(row["request_seconds"] for row in rows)
        if rows and all(isinstance(row.get("request_seconds"), (int, float)) for row in rows)
        else None,
    }


def source_artifact(checkpoints: Any, source: Any) -> str | None:
    blob = getattr(source, "blob", None)
    if not isinstance(blob, str):
        return None
    path = checkpoints.store.root / "blobs" / blob[:2] / blob
    return str(path) if path.is_file() else None


def _path(checkpoints: Any, directory: str, key: str, suffix: str) -> Path:
    root = checkpoints.store.owned_path(checkpoints.root / directory)
    root.mkdir(parents=True, exist_ok=True)
    return checkpoints.store.owned_path(root / f"{valid_id(key)}{suffix}")


def _overview_text(fragments: list[str]) -> str:
    seen: set[str] = set()
    paragraphs: list[str] = []
    for fragment in fragments:
        for paragraph in fragment.strip().split("\n\n"):
            item = paragraph.strip()
            if item and item not in seen:
                paragraphs.append(item)
                seen.add(item)
    return "\n\n".join(paragraphs) + ("\n" if paragraphs else "")


def ordered_fragments(state: dict[str, Any]) -> list[str]:
    """Keep accepted parent fragments in source order after a window splits."""
    positioned = [
        (row["start"], row["text"])
        for row in state.get("retained_fragments", [])
        if isinstance(row, dict)
    ]
    positioned.extend(
        (window["target_start"], state["fragments"][wid])
        for window in state["windows"]
        if (wid := window_receipt_id(window)) in state["fragments"]
    )
    return [text for _, text in sorted(positioned, key=lambda row: row[0])]


def _target_ranges(window: dict[str, Any], parsed: Any) -> list[Any]:
    target = window.get("target_ranges") or [[window["target_start"], window["target_end"]]]
    ranges, _ = document_planning_support.exclude_attachment_ranges(parsed, target)
    return ranges


def _task_id(window: dict[str, Any], subtask: str) -> str:
    return window_receipt_id(window) + ":" + subtask


def _omissions(
    state: dict[str, Any], windows: list[dict[str, Any]], parsed: Any
) -> list[PlanningOmission]:
    rows: list[PlanningOmission] = []
    targets = [
        (_task_id(window, subtask), subtask, _target_ranges(window, parsed))
        for window in windows
        for subtask in (("overview",) if state.get("planning_strategy") else ("overview", "pages"))
    ]
    targets.extend(
        (
            key,
            "pages",
            [value for row in state["tasks"][key]["sections"] for value in row["original_ranges"]]
            or [[0, len(parsed.blocks)]],
        )
        for key in state.get("planning_tasks", [])
    )
    for key, subtask, ranges in targets:
        task = state["tasks"].get(key, {})
        reason = task.get("reason")
        if not reason or not ranges:
            continue
        rows.append(
            PlanningOmission(
                key="omission:" + content_id((key, reason))[:24],
                stage="planning",
                reason=reason,
                target_id=key,
                ranges=ranges,
                affected_pages=[],
                component=subtask,
                attempts=task.get("attempts", 0),
                diagnostic_ref=None,
            )
        )
    return rows


def _finalize(
    checkpoints: Any,
    key: str,
    state: dict[str, Any],
    source: Any,
    parsed: Any,
    navigation: Any,
    settings: dict[str, Any],
    entity_types: list[str],
    existing_targets: set[str],
    plan_only: bool,
    started: float,
) -> PlanningResult:
    from openkb.agent.document_plan import to_dict
    from openkb.agent.document_plan_preview import render_plan_preview

    windows = state["windows"]
    from openkb.agent.document_planning_overview import current_overview, overview_summary

    overview = current_overview(state)
    overview_ref = None
    if overview:
        overview_path = _path(checkpoints, "overview", key, ".md")
        atomic_write_text(overview_path, overview)
        overview_ref = str(overview_path)
    pages = [PagePlan.from_dict(item) for item in state["pages"]]
    deferred = state.get("deferred_suggestions", [])
    from openkb.agent.document_planning_quality import quality_summary

    quality = quality_summary(state, overview)
    has_products = bool(overview or pages or deferred)
    omissions = _omissions(state, windows, parsed)
    failed = bool(omissions)
    outcome = "partial" if has_products and failed else "complete" if has_products else "empty"
    if (pages or deferred) and not overview:
        outcome = "partial"
    if state.get("budget_limited"):
        outcome = "budget_limited"
    metadata = {
        "protocol": "document-plan-v4",
        "planning_semantics": "tolerant-quality-v2",
        "planning_strategy": state.get("planning_strategy"),
        "planning_mode": state.get("planning_mode"),
        "planning_snapshot": state.get("planning_snapshot"),
        "deferred_suggestions": deferred,
        "suggestion_annotations": state.get("suggestion_annotations", {}),
        "batch_notes": state.get("batch_notes", []),
        "promoted_suggestions": state.get("promoted_suggestions", []),
        "overview_snapshot": state.get("overview_snapshot"),
        "overview_history": state.get("overview_history", []),
        **quality,
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "navigation": navigation.get("id") if navigation else None,
        "recovery_key": key,
        "plan_only": plan_only,
        "outcome": outcome,
        "overview_ref": overview_ref,
        "overview": overview_summary(state),
        "completed_windows": len(windows),
        "entity_types": entity_types,
        "catalog_targets": state.get("catalog_targets", sorted(existing_targets)),
        "planning_execution": {
            "planning_requests": len(state["request_usage"])
            if state["request_usage"]
            else state["attempts"],
            "recovery_requests": state.get("recovery_requests", 0),
            "elapsed_seconds": max(0.0, time.monotonic() - started),
            **usage_totals(state),
            "overview_tasks": len(windows),
            "pages_tasks": len(state.get("planning_tasks", [])),
            "by_component": {
                name: usage_totals(
                    {
                        "request_usage": [
                            row
                            for row in state.get("request_usage", [])
                            if row.get("planning_component") == name
                        ]
                    }
                )
                for name in ("overview", "pages")
            },
        },
    }
    plan = None
    if has_products:
        plan = DocumentPlan(
            metadata=metadata,
            overview=OverviewPlan(
                text=overview,
                status="complete"
                if overview
                and all(
                    state["tasks"].get(_task_id(window, "overview"), {}).get("status") == "accepted"
                    for window in windows
                )
                else "partial",
            ),
            pages=pages,
            planning_omissions=omissions,
            external_references=[
                ExternalReference.from_dict(row) for row in state.get("external_references", [])
            ],
        )
        from openkb.agent.document_recovery import inherit_publication_state

        saved_plan = checkpoints.load_recovery(key, "plan")
        inherit_publication_state(plan, saved_plan)
        validate_plan(plan, parsed, entity_types, existing_targets)
        from openkb.agent.document_plan_proof_reader import MARKDOWN_REFERENCE_PROOF_SYSTEM

        reference_rows = [row.to_dict() for row in plan.external_references]
        proof_payload = {
            "stage": "planning",
            "subtask": "accepted_reference_proof",
            "recovery_key": key,
            "reference_digest": content_id(reference_rows),
        }
        with checkpoints.request(MARKDOWN_REFERENCE_PROOF_SYSTEM, proof_payload) as proof_key:
            checkpoints.save(proof_key, reference_rows)
        plan.metadata["reference_proof_key"] = proof_key
    coverage = planning_coverage(plan, parsed, omission_count=len(omissions), outcome=outcome)
    page_scopes = planning_page_scopes(plan, parsed)
    retained_starts = {
        row["start"] for row in state.get("retained_fragments", []) if isinstance(row, dict)
    }

    def has_overview_fragment(window: dict[str, Any]) -> bool:
        return (
            window_receipt_id(window) in state["fragments"]
            or window["target_start"] in retained_starts
        )

    report_path = _path(checkpoints, "plan-report", key, ".json")
    if plan is not None:
        plan.metadata["planning_coverage"] = coverage
        plan.metadata["page_scopes"] = page_scopes
        preview_path = _path(checkpoints, "plan-preview", key, ".md")
        plan.metadata["plan_preview"] = str(preview_path)
        plan.metadata["plan_report"] = str(report_path)
        atomic_write_text(preview_path, render_plan_preview(plan))
        checkpoints.save_recovery(key, "plan", to_dict(plan))
    report = {
        "protocol": "document-planning-report-v2",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "source_origin": getattr(source, "origin", None),
        "source_artifact": source_artifact(checkpoints, source),
        "parse_artifact": str(checkpoints.store.root / "parses" / f"{parsed.id}.json")
        if (checkpoints.store.root / "parses" / f"{parsed.id}.json").is_file()
        else None,
        "navigation_id": navigation.get("id") if navigation else None,
        "outcome": outcome,
        "planning_strategy": state.get("planning_strategy"),
        "planning_mode": state.get("planning_mode"),
        "planning_snapshot": state.get("planning_snapshot"),
        "overview": overview_summary(state),
        **quality,
        "overview_ref": overview_ref,
        "plan_preview": plan.metadata["plan_preview"] if plan is not None else None,
        "pages": len(pages),
        "suggestions": {
            "accepted": len(pages),
            "deferred": len(deferred),
            "merged": sum(row.get("reason") == "merged_page" for row in state["filtered"]),
            "dropped": sum(row.get("reason") == "dropped_item" for row in state["rejected"]),
            "promoted": len(state.get("promoted_suggestions", [])),
            "explanations": len(state.get("batch_notes", [])),
        },
        "page_states": {
            name: sum(page.state == name for page in pages)
            for name in ("pending_evidence", "ready", "skipped")
        },
        "page_quality": {
            name: sum(page.quality == name for page in pages) for name in ("generated", "published")
        },
        "source_queryable": _source_queryable(checkpoints.store.kb_dir, source, parsed),
        "no_pages_recommended": bool(windows)
        and not pages
        and not deferred
        and all(
            state["tasks"][key].get("no_pages_recommended")
            for key in state.get("planning_tasks", [])
        ),
        "overview_targets": {
            "complete": sum(
                state["tasks"].get(_task_id(window, "overview"), {}).get("status") == "accepted"
                for window in windows
            ),
            "partial": sum(
                state["tasks"].get(_task_id(window, "overview"), {}).get("status") != "accepted"
                and has_overview_fragment(window)
                for window in windows
            ),
            "failed": sum(
                state["tasks"].get(_task_id(window, "overview"), {}).get("status") == "skipped"
                and not has_overview_fragment(window)
                for window in windows
            ),
            "total": len(windows),
        },
        "windows_processed": len(windows),
        "windows_total": len(windows),
        "parser_gaps": coverage["parser_gaps"],
        "external_references": len(state["external_references"]),
        "downstream": "not_started_at_planning_handoff",
        "retry_skipped_available": True,
        "planning_omissions": [item.to_dict() for item in omissions],
        "rejected_candidates": rejected_candidate_summary(state),
        "rejected_candidate_history": state["rejected"],
        "filtered_candidates": {
            reason: sum(row.get("reason") == reason for row in state.get("filtered", []))
            for reason in sorted(
                {row.get("reason") for row in state.get("filtered", []) if row.get("reason")}
            )
        },
        "filtered_candidate_history": state.get("filtered", []),
        "planning_coverage": coverage,
        "page_scopes": page_scopes,
        "planning_execution": metadata["planning_execution"],
        "tasks": {
            task_id: {field: value for field, value in task.items() if field != "raw"}
            for task_id, task in state["tasks"].items()
            if task.get("status") != "retired"
        },
        "retired_tasks": {
            key: task for key, task in state["tasks"].items() if task.get("status") == "retired"
        },
    }
    atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    checkpoints.save_recovery(key, "plan_report", report)
    record_document_totals(planned_pages=len(pages))
    return PlanningResult(
        plan,
        outcome,
        str(report_path),
        overview_ref,
        tuple(item.to_dict() for item in omissions),
    )


def _source_queryable(kb_dir: Path, source: Any, parsed: Any) -> bool:
    from openkb.query_sources import query_source_bindings
    from openkb.state import HashRegistry

    binding = query_source_bindings(kb_dir).get(source.source_id) or HashRegistry(
        kb_dir / ".openkb/hashes.json"
    ).get(source.source_id)
    return bool(
        binding
        and binding.get("navigation_id")
        and binding.get("source_version") == source.id
        and binding.get("parse_id") == parsed.id
    )


def refresh_execution_report(checkpoints: Any, plan: DocumentPlan) -> None:
    """Update the planning handoff with actual preparation/generation progress."""
    key = plan.metadata["recovery_key"]
    report = checkpoints.load_recovery(key, "plan_report")
    if not isinstance(report, dict):
        return
    report["page_states"] = {
        state: sum(p.state == state for p in plan.pages)
        for state in ("pending_evidence", "ready", "skipped")
    }
    report["page_quality"] = {
        "generated": sum(p.quality != "planned" for p in plan.pages),
        "published": sum(p.quality == "published" for p in plan.pages),
    }
    report["downstream"] = "evidence_prepared"
    report["evidence_scopes"] = {page.key: page.evidence_scope for page in plan.pages}
    report.setdefault("location_bindings", {})["actual_evidence_ready"] = sum(
        p.state == "ready" for p in plan.pages
    )
    from openkb.evidence import ParseStore

    parsed = ParseStore(checkpoints.store.kb_dir).load(plan.metadata["parse_id"])
    report["page_scopes"] = planning_page_scopes(plan, parsed)
    report["planning_coverage"] = planning_coverage(plan, parsed)
    plan.metadata["planning_coverage"] = report["planning_coverage"]
    plan.metadata["page_scopes"] = report["page_scopes"]
    checkpoints.save_recovery(key, "plan_report", report)
    atomic_write_text(
        _path(checkpoints, "plan-report", key, ".json"),
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )
    from openkb.agent.document_plan_preview import render_plan_preview

    atomic_write_text(_path(checkpoints, "plan-preview", key, ".md"), render_plan_preview(plan))
