"""Navigation overview followed by global Markdown page selection."""

from __future__ import annotations

import time
from typing import Any, Callable

from openkb.agent import document_planning_support
from openkb.agent.document_plan import PagePlan, RangeValue
from openkb.agent.document_plan_annotations import ExternalReference
from openkb.agent.document_planning_admission import validate_navigation
from openkb.agent.document_planning_locations import section_contribution
from openkb.agent.document_planning_report import (
    _finalize,
)
from openkb.agent.document_planning_result import PlanningResult
from openkb.agent.document_planning_state import _persist, _state
from openkb.agent.document_planning_state import _valid_state as _valid_state
from openkb.config import compilation_model_options, resolve_entity_types
from openkb.execution_measurement import (
    record_document_totals,
)
from openkb.implementation import module_revision
from openkb.processing import (
    ProcessingIncomplete,
    RequestLimits,
    processing_checkpoint,
)
from openkb.schema import get_agents_md
from openkb.sources import content_id


def _navigation_rows(
    hints: list[dict[str, Any]],
    navigation: Any,
    parsed: Any,
    *,
    target: list[RangeValue] | None = None,
    evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    nodes = navigation.get("nodes", []) if isinstance(navigation, dict) else []
    by_key = {f"section:{node.get('id', index)}": node for index, node in enumerate(nodes)}
    rows = []
    for hint in hints:
        section_key = hint.get("section_key")
        node = by_key.get(section_key) if isinstance(section_key, str) else None
        if node is None:
            continue
        row = dict(hint)
        row["original_ranges"], _ = document_planning_support.exclude_attachment_ranges(
            parsed, [[node["start"], node.get("end", node["start"] + 1)]]
        )
        if target is not None and evidence is not None:
            row["subject_ranges"] = section_contribution(
                row["original_ranges"], target, parsed, evidence
            )
        rows.append(row)
    return rows


def _catalog_window(
    entries: list[tuple[str, str, str]], evidence: dict[str, Any], hints: list[dict[str, Any]]
) -> list[tuple[str, str, str]]:
    context = " ".join(
        str(block.get("text", ""))[:500] for block in evidence.get("blocks", [])[:30]
    ) + " ".join(str(hint.get("title", "")) for hint in hints)
    ranked = sorted(
        entries,
        key=lambda row: (row[1].casefold() not in context.casefold(), row[0]),
    )
    return ranked[:40]


def _accepted_overview(state: dict[str, Any]) -> str:
    from openkb.agent.document_planning_overview import current_overview

    return current_overview(state)


class _Response(str):
    finish_reason: str

    def __new__(cls, content: str, finish_reason: str) -> _Response:
        instance = super().__new__(cls, content)
        instance.finish_reason = finish_reason
        return instance


def _call(
    messages: Any,
    settings: dict[str, Any],
    limits: RequestLimits,
    bundle: Any,
    mock_caller: Callable[..., Any] | None,
    subtask: str,
) -> Any:
    if mock_caller is not None:
        return mock_caller(messages, settings=settings)
    from openkb.agent.compiler import _llm_call
    from openkb.processing import active_request_limits

    options = compilation_model_options(settings, stage="planning")
    options.pop("response_format", None)
    active_limits = active_request_limits()
    admitted, _ = (active_limits or limits).request(settings["model"], messages, options)
    if active_limits is not None:
        # The execution budget re-admits an expanded retry against its new
        # limits; an explicit cap here would pin that retry to the old size.
        admitted.pop("max_tokens", None)
    return _llm_call(
        settings["model"],
        messages,
        f"planning_{subtask}",
        bundle=bundle,
        decode_response=False,
        **admitted,
    )


def _source_references(
    evidence: dict[str, Any], navigation: Any, parsed: Any, pages: list[PagePlan]
) -> list[ExternalReference]:
    """Register external mentions only when their quote is in supplied original text."""
    from openkb.agent.document_reference_check import detect_references

    by_id = {row["id"]: row for row in evidence.get("blocks", []) if "id" in row}
    delta = {
        "page_changes": [
            {"local_key": page.key, "subject_ranges": page.subject_ranges} for page in pages
        ]
    }
    result = []
    by_page = {page.key: page for page in pages}
    for candidate in detect_references(evidence, navigation, delta, parsed):
        if (
            candidate.availability in {"in_window", "outside_window"}
            and len(candidate.target_options) == 1
        ):
            basis = candidate.basis_ranges[0] if candidate.basis_ranges else {}
            source_block = by_id.get(basis.get("block"), {})
            body = str(source_block.get("text", ""))
            offset = (source_block.get("reference") or {}).get("start", 0)
            local_start = max(0, basis.get("start_char", 0) - offset)
            sentence_start = max(body.rfind("。", 0, local_start), body.rfind(".", 0, local_start))
            sentence_end = body.find("。", local_start)
            sentence = body[sentence_start + 1 : sentence_end + 1 if sentence_end >= 0 else None]
            if any(marker in sentence for marker in ("不影响", "仅供参考", "扩展阅读")):
                continue
            node = candidate.target_options[0]
            context_ranges, _ = document_planning_support.exclude_attachment_ranges(
                parsed, [[node["start"], node["end"]]]
            )
            for page_key in candidate.affected_page_refs:
                page = by_page.get(page_key)
                if page is not None:
                    for value in context_ranges:
                        if value not in page.context_ranges and value not in page.subject_ranges:
                            page.context_ranges.append(value)
        if candidate.availability != "external_not_supplied":
            continue
        ranges: list[RangeValue] = []
        quotes = []
        for basis in candidate.basis_ranges:
            block = by_id.get(basis.get("block"))
            if not isinstance(block, dict):
                break
            index = block["order"]
            start, end = basis["start_char"], basis["end_char"]
            offset = (block.get("reference") or {}).get("start", 0)
            if (
                type(index) is not int
                or not 0 <= index < len(parsed.blocks)
                or "attachment" in parsed.blocks[index].location
                or not 0 <= start < end <= parsed.blocks[index].chars
            ):
                break
            ranges.append({"block_index": index, "start_char": start, "end_char": end})
            quotes.append(block["text"][start - offset : end - offset])
        else:
            if ranges and all(quotes):
                result.append(
                    ExternalReference(
                        key="xref:" + candidate.reference_key,
                        location=ranges,
                        raw_quote="\n".join(quotes),
                        target_document=candidate.target_text,
                        target_section=None,
                        affected_pages=list(candidate.affected_page_refs),
                    )
                )
    return result


def plan_markdown_document(
    kb_dir: Any,
    workspace: Any,
    source: Any,
    parsed: Any,
    navigation: dict[str, Any] | None,
    settings: dict[str, Any],
    checkpoints: Any,
    *,
    bundle: Any = None,
    on_event: Callable[[dict[str, Any]], None],
    resume: bool = False,
    retry_skipped: bool = False,
    plan_only: bool = False,
    mock_caller: Callable[..., Any] | None = None,
) -> PlanningResult:
    """Freeze navigation once, then settle overview and page-selection tasks."""
    started = time.monotonic()
    processing_checkpoint("planning")
    validate_navigation(navigation, source, parsed)
    if parsed.blocks and not (navigation or {}).get("nodes"):
        # A parse-only caller still has a truthful document-level location.
        # No source text is manufactured or marked as read by this fallback.
        navigation = {
            "nodes": [
                {
                    "id": "source",
                    "parent": None,
                    "title": getattr(source, "name", source.source_id),
                    "start": 0,
                    "end": len(parsed.blocks),
                    "summary": "",
                    "summary_origin": "unavailable",
                }
            ]
        }
    wiki = (workspace.path if hasattr(workspace, "path") else workspace) / "wiki"
    schema = get_agents_md(wiki)
    entity_types = resolve_entity_types(settings)
    source_conditions = document_planning_support.source_conditions(parsed)
    from openkb.agent.document_planning_runtime import read_planning_inputs
    from openkb.navigation_metadata import structure_diagnostics

    limits = RequestLimits.from_config(settings)
    inputs = read_planning_inputs(kb_dir, wiki, source, parsed, settings, limits)
    existing_targets = set(inputs["catalog_targets"])
    catalog_entries, catalog_types = inputs["catalog"], inputs["catalog_types"]
    catalog_metadata, runtime = inputs["catalog_metadata"], inputs["runtime"]
    windows: list[dict[str, Any]] = []
    planning_limits = limits
    record_document_totals(evidence_groups=0)
    identity = {
        "source": source.source_id,
        "version": source.id,
        "parse": parsed.id,
        "navigation": content_id(navigation.get("nodes", [])) if navigation else None,
        "settings": {
            field: settings.get(field)
            for field in (
                "model",
                "language",
                "entity_types",
                "default_entity_type",
                "planning_thinking",
                "planning_reasoning_effort",
                "processing",
            )
        },
        "schema": content_id(schema),
        "catalog": catalog_entries,
        "catalog_targets": sorted(
            target for target in existing_targets if target.startswith(("concepts/", "entities/"))
        ),
        "catalog_types": catalog_types,
        "catalog_metadata": catalog_metadata,
        "runtime": runtime,
        "runtime_rules": module_revision("openkb.agent.document_planning_runtime"),
        "source_conditions": source_conditions,
        "structure_diagnostics": structure_diagnostics(navigation or {}),
        "rules": module_revision("openkb.agent.document_protocol"),
        "response": module_revision("openkb.agent.document_planning_response"),
        "markdown": module_revision("openkb.agent.document_planning_markdown"),
        "locations": module_revision("openkb.agent.document_planning_locations"),
        "candidates": module_revision("openkb.agent.document_planning_candidates"),
        "pages": module_revision("openkb.agent.document_planning_pages"),
        "planner": module_revision(__name__),
        "catalog_rules": module_revision("openkb.agent.document_planning_catalog"),
        "report": module_revision("openkb.agent.document_planning_report"),
        "projection": module_revision("openkb.agent.document_planning_projection"),
        "semantics": module_revision("openkb.agent.document_planning_semantics"),
        "state": module_revision("openkb.agent.document_planning_state"),
        "bindings": module_revision("openkb.agent.document_planning_bindings"),
        "carry": module_revision("openkb.agent.document_planning_carry"),
        "overview": module_revision("openkb.agent.document_planning_overview"),
        "prompts": module_revision("openkb.agent.document_markdown_prompts"),
        "quality": module_revision("openkb.agent.document_planning_quality"),
        "navigation_overview": module_revision("openkb.agent.document_navigation_overview"),
        "global_context": module_revision("openkb.agent.document_global_context"),
        "global_planning": module_revision("openkb.agent.document_global_planning"),
        "source_protocol": module_revision("openkb.agent.source_protocol"),
    }
    if resume:
        from openkb.agent.document_planning_catalog import retained_catalog

        retained = retained_catalog(kb_dir, wiki, source, parsed, checkpoints, identity)
        if retained is not None:
            previous_key, previous_catalog = retained
            candidate = {**identity, **previous_catalog}
            if checkpoints.identity("document-planning-navigation-v3", candidate) == previous_key:
                identity = candidate
                catalog_entries = [tuple(row) for row in previous_catalog["catalog"]]
                catalog_types = previous_catalog["catalog_types"]
                catalog_metadata = previous_catalog["catalog_metadata"]
                runtime = previous_catalog["runtime"]
                existing_targets = set(previous_catalog["catalog_targets"])
    key = checkpoints.identity("document-planning-navigation-v3", identity)
    state = _state(checkpoints, key, windows, resume)
    # Accepted references are re-derived from source evidence on every run.
    # Mutable resume state must not mint a new immutable proof for altered rows.
    state["external_references"] = []
    state.setdefault("catalog_targets", sorted(existing_targets))
    state.setdefault("catalog_types", catalog_types)
    for field, expected in (("catalog_metadata", catalog_metadata), ("runtime", runtime)):
        if field in state and state[field] != expected:
            raise ProcessingIncomplete("planning_recovery_identity_mismatch", "planning")
        state[field] = expected

    if retry_skipped:
        for task in state["tasks"].values():
            if task.get("status") == "skipped":
                task.update(status="pending", attempts=0, raw=None, reason=None)
        state.pop("budget_limited", None)
    from openkb.agent.document_global_context import freeze_context
    from openkb.agent.document_navigation_overview import plan_navigation_overview
    from openkb.agent.document_planning_budget import PlanningBudget

    budget = PlanningBudget(planning_limits, settings, mock=mock_caller is not None)
    freeze_context(
        state,
        navigation,
        source,
        parsed,
        catalog_entries,
        settings,
        planning_limits,
        entity_types,
        schema,
        source_conditions,
    )
    _persist(checkpoints, key, state)
    if parsed.blocks and not document_planning_support.no_readable_body(parsed):
        plan_navigation_overview(
            state,
            checkpoints,
            key,
            source,
            parsed,
            settings,
            planning_limits,
            entity_types,
            schema,
            source_conditions,
            budget,
            bundle=bundle,
            mock_caller=mock_caller,
        )
    from openkb.agent.document_global_planning import plan_global_pages

    try:
        if parsed.blocks and not document_planning_support.no_readable_body(parsed):
            plan_global_pages(
                state,
                checkpoints,
                key,
                source,
                parsed,
                navigation,
                catalog_entries,
                settings,
                planning_limits,
                entity_types,
                schema,
                source_conditions,
                existing_targets,
                budget,
                bundle=bundle,
                mock_caller=mock_caller,
            )
    except ProcessingIncomplete as exc:
        if exc.reason not in {
            "request_budget_exhausted",
            "token_budget_exhausted",
            "time_budget_exhausted",
        }:
            raise
        state["budget_limited"] = exc.reason
        for task_key in state.get("planning_tasks", []):
            task = state["tasks"][task_key]
            if task["status"] not in {"accepted", "retired"}:
                task.update(status="skipped", reason=exc.reason)
    from contextlib import nullcontext

    from openkb.processing import budget_settlement_scope

    with budget_settlement_scope() if state.get("budget_limited") else nullcontext():
        _persist(checkpoints, key, state)
        result = _finalize(
            checkpoints,
            key,
            state,
            source,
            parsed,
            navigation,
            settings,
            entity_types,
            existing_targets,
            plan_only,
            started,
        )
    on_event(
        {
            "stage": "planning",
            "status": result.outcome,
            "pages": len(result.plan.pages) if result.plan else 0,
            "report": result.report_ref,
            "overview": result.overview_ref,
        }
    )
    return result
