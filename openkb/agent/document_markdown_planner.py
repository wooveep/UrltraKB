"""Cumulative window overviews followed by global Markdown page selection."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from openkb import frontmatter
from openkb.agent import document_planning_support
from openkb.agent.document_plan import PagePlan, RangeValue
from openkb.agent.document_plan_annotations import ExternalReference
from openkb.agent.document_planning_admission import admit_planning_windows, validate_navigation
from openkb.agent.document_planning_locations import section_contribution
from openkb.agent.document_planning_report import (
    _finalize,
    _path,
    _target_ranges,
    _task_id,
)
from openkb.agent.document_planning_response import accept_overview
from openkb.agent.document_planning_result import PlanningResult
from openkb.agent.document_planning_state import _persist, _raw_value, _split_window, _state
from openkb.agent.document_planning_state import _valid_state as _valid_state
from openkb.config import compilation_model_options, resolve_entity_types
from openkb.execution_measurement import (
    record_document_totals,
    record_first_inspectable,
    request_marker,
    request_usage_since,
)
from openkb.implementation import module_revision
from openkb.lint import list_existing_wiki_targets
from openkb.locks import atomic_write_text
from openkb.processing import (
    InputTooLarge,
    OutputTruncated,
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


def _catalog_entries(wiki: Path, targets: set[str]) -> list[tuple[str, str, str]]:
    """Read bounded titles and previews for safe existing-page matching."""
    entries = []
    for target in sorted(targets):
        if not target.startswith(("concepts/", "entities/")):
            continue
        path = wiki / f"{target}.md"
        if path.is_symlink():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        body = (frontmatter.split(content) or ("", content))[1]
        heading = next(
            (
                match[1].strip()
                for line in body.splitlines()[:20]
                if (match := re.match(r"^#\s+(.+)$", line))
            ),
            "",
        )
        metadata_title = frontmatter.parse(content).get("title")
        title = metadata_title if isinstance(metadata_title, str) and metadata_title else heading
        if not title:
            title = target.rsplit("/", 1)[-1].replace("-", " ")
        snippet = next(
            (
                line.strip()
                for line in body.splitlines()
                if line.strip() and not line.startswith("#")
            ),
            "",
        )[:120]
        entries.append((target, title, snippet))
    return entries


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
    """Settle all overview windows before freezing the global planning context."""
    started = time.monotonic()
    processing_checkpoint("planning")
    validate_navigation(navigation, source, parsed)
    wiki = (workspace.path if hasattr(workspace, "path") else workspace) / "wiki"
    schema = get_agents_md(wiki)
    entity_types = resolve_entity_types(settings)
    source_conditions = document_planning_support.source_conditions(parsed)
    limits = RequestLimits.from_config(settings)
    windows, planning_limits = admit_planning_windows(
        source,
        parsed,
        navigation,
        settings,
        limits,
        entity_types=entity_types,
        schema=schema,
        parser_conditions=source_conditions,
    )
    record_document_totals(evidence_groups=len(windows))
    key = checkpoints.identity(
        "document-planning-markdown-v1",
        {
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
            "rules": module_revision("openkb.agent.document_protocol"),
            "response": module_revision("openkb.agent.document_planning_response"),
            "locations": module_revision("openkb.agent.document_planning_locations"),
            "candidates": module_revision("openkb.agent.document_planning_candidates"),
            "pages": module_revision("openkb.agent.document_planning_pages"),
            "planner": module_revision(__name__),
            "report": module_revision("openkb.agent.document_planning_report"),
            "projection": module_revision("openkb.agent.document_planning_projection"),
            "semantics": module_revision("openkb.agent.document_planning_semantics"),
            "state": module_revision("openkb.agent.document_planning_state"),
            "bindings": module_revision("openkb.agent.document_planning_bindings"),
            "carry": module_revision("openkb.agent.document_planning_carry"),
            "overview": module_revision("openkb.agent.document_planning_overview"),
            "prompts": module_revision("openkb.agent.document_markdown_prompts"),
            "quality": module_revision("openkb.agent.document_planning_quality"),
            "global_context": module_revision("openkb.agent.document_global_context"),
            "global_planning": module_revision("openkb.agent.document_global_planning"),
            "source_protocol": module_revision("openkb.agent.source_protocol"),
        },
    )
    state = _state(checkpoints, key, windows, resume)
    # Accepted references are re-derived from source evidence on every run.
    # Mutable resume state must not mint a new immutable proof for altered rows.
    state["external_references"] = []
    existing_targets = list_existing_wiki_targets(wiki)
    catalog_entries = _catalog_entries(wiki, existing_targets)
    state.setdefault("catalog_targets", sorted(existing_targets))
    if retry_skipped:
        if any(
            task.get("status") == "skipped"
            for name, task in state["tasks"].items()
            if name.endswith(":overview")
        ):
            state.pop("planning_snapshot", None)
            for task_key in state.pop("planning_tasks", []):
                state["tasks"][task_key]["status"] = "retired"
        state.pop("budget_limited", None)
        for task in state["tasks"].values():
            if task.get("status") == "skipped":
                task.update(status="pending", attempts=0, reason=None, raw=None)
        state["rejected"] = []
        _persist(checkpoints, key, state)
    from openkb.agent.document_global_context import freeze_context, messages_for
    from openkb.agent.document_planning_budget import PlanningBudget

    budget = PlanningBudget(planning_limits, settings, mock=mock_caller is not None)

    def pages_reservation():
        temporary = {**state, "planning_snapshot": None}
        snapshot = freeze_context(
            temporary,
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
        messages = messages_for(
            snapshot,
            snapshot["nodes"][:1],
            temporary,
            source,
            parsed,
            settings,
            planning_limits,
            entity_types,
            schema,
            source_conditions,
        )
        try:
            return budget.cost(messages)
        except InputTooLarge:
            return planning_limits.input_capacity + planning_limits.output_tokens

    index = 0
    try:
        while index < len(state["windows"]):
            window = state["windows"][index]
            processing_checkpoint("planning")
            desc = window.get("evidence")
            if desc is None:
                from openkb.navigation_evidence import evidence_descriptor

                desc = evidence_descriptor(
                    source, parsed, window["target_start"], window["target_end"]
                )
            try:
                evidence = document_planning_support.read_target_evidence(
                    kb_dir,
                    source,
                    parsed,
                    desc,
                    None
                    if window.get("evidence") is not None
                    else window.get("frozen_ranges") or window.get("target_ranges"),
                )
            except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
                if mock_caller is None:
                    raise ProcessingIncomplete("planned_evidence_unavailable", "planning") from exc
                evidence = document_planning_support.fallback_read_evidence(
                    source, parsed, window["target_start"], window["target_end"]
                )
            try:
                hints = document_planning_support.select_navigation_hints(
                    navigation,
                    _target_ranges(window, parsed),
                    planning_limits,
                    evidence=evidence,
                    parsed=parsed,
                    model=settings["model"],
                )
            except ProcessingIncomplete as exc:
                if exc.reason != "planning_navigation_exceeds_request_budget":
                    raise
                if _split_window(state, index, source, parsed, limits):
                    _persist(checkpoints, key, state)
                    continue
                hints = []
                for subtask in ("overview",):
                    state["tasks"][_task_id(window, subtask)] = {
                        "status": "skipped",
                        "attempts": 0,
                        "raw": None,
                        "reason": "planning_navigation_exceeds_request_budget",
                    }
                _persist(checkpoints, key, state)
            target = _target_ranges(window, parsed)
            selected_catalog = _catalog_window(catalog_entries, evidence, hints)
            needs_split = False
            for subtask in ("overview",):
                task_key = _task_id(window, subtask)
                task = state["tasks"].setdefault(
                    task_key,
                    {
                        "status": "pending",
                        "attempts": 0,
                        "raw": None,
                        "reason": None,
                    },
                )
                if task["status"] in {"accepted", "skipped"}:
                    continue
                max_attempts = limits.max_attempts
                while (
                    task["attempts"] < max_attempts
                    or task.get("raw") is not None
                    or task.get("replay")
                ):
                    processing_checkpoint("planning")
                    recovery = task.get("reason") or ""
                    overview = _accepted_overview(state)
                    displayed_targets = [row[0] for row in selected_catalog]
                    catalog_text = "\n".join(
                        f"- {path} | {title}" + (f" — {brief}" if brief else "")
                        for path, title, brief in selected_catalog
                    )
                    from openkb.agent.document_planning_carry import project_messages

                    state["displayed_targets"] = displayed_targets
                    messages, projection = project_messages(
                        evidence,
                        state,
                        overview,
                        {
                            "target_start": window["target_start"],
                            "target_end": window["target_end"],
                            "total_blocks": len(parsed.blocks),
                            "ranges": target,
                        },
                        hints,
                        catalog_text,
                        entity_types,
                        schema,
                        settings,
                        planning_limits,
                        source_conditions=source_conditions,
                        subtask=subtask,
                        recovery=recovery,
                    )
                    if task.get("raw") is None and task.get("replay"):
                        task["raw"] = task["replay"].pop(0)
                        state["responses"].append({"task": task_key, "attempt": 0, **task["raw"]})
                    if task.get("raw") is None:
                        reserved_tokens = pages_reservation()
                        budget.admit(messages, reserve_pages=reserved_tokens)
                        task["attempts"] += 1
                        state["attempts"] += 1
                        if task["attempts"] > 1:
                            state["recovery_requests"] = state.get("recovery_requests", 0) + 1
                        _persist(checkpoints, key, state)
                        marker = request_marker()
                        from openkb.agent.document_planning_bindings import capture_request

                        request_binding = capture_request(
                            messages, source, parsed, window, checkpoints.store
                        )
                        task["input_ranges"] = [row["range"] for row in request_binding["aliases"]]
                        try:
                            from openkb.processing_reservation import reserve_later_work

                            with reserve_later_work(
                                requests=1,
                                tokens=reserved_tokens,
                                attempts=max_attempts - task["attempts"] + 1,
                            ):
                                raw = _call(
                                    messages,
                                    settings,
                                    planning_limits,
                                    bundle,
                                    mock_caller,
                                    subtask,
                                )
                        except InputTooLarge:
                            task["attempts"] -= 1
                            state["attempts"] -= 1
                            if task["attempts"] > 0:
                                state["recovery_requests"] -= 1
                            if selected_catalog:
                                selected_catalog = selected_catalog[: len(selected_catalog) // 2]
                                _persist(checkpoints, key, state)
                                continue
                            task["reason"] = "planning_context_capacity"
                            _persist(checkpoints, key, state)
                            needs_split = True
                            break
                        except OutputTruncated:
                            task["reason"] = "model_output_truncated"
                            planning_limits = planning_limits.expanded(
                                reason="output_budget_exhausted"
                            )
                            _persist(checkpoints, key, state)
                            continue
                        except ProcessingIncomplete as exc:
                            if exc.reason != "planning_recovery_budget":
                                raise
                            task["reason"] = exc.reason
                            break
                        finally:
                            dispatched = max(0, request_marker() - marker)
                            state.setdefault("request_usage", []).extend(
                                {**row, "planning_component": "overview", "task": task_key}
                                for row in request_usage_since(marker)
                            )
                            if dispatched > 1:
                                task["attempts"] += dispatched - 1
                                state["attempts"] += dispatched - 1
                                state["recovery_requests"] = (
                                    state.get("recovery_requests", 0) + dispatched - 1
                                )
                                _persist(checkpoints, key, state)
                        task["raw"] = {
                            **_raw_value(raw),
                            "binding": request_binding["id"],
                            "projection": projection,
                        }
                        state.setdefault("responses", []).append(
                            {
                                "task": task_key,
                                "attempt": task["attempts"],
                                **task["raw"],
                            }
                        )
                        _persist(checkpoints, key, state)
                    response = _Response(task["raw"]["content"], task["raw"]["finish_reason"])
                    if subtask == "overview":
                        overview_result = accept_overview(response)
                        if overview_result.text:
                            from openkb.agent.document_planning_overview import accept_snapshot

                            accept_snapshot(state, window, target, task["raw"], overview_result)
                            task["status"] = "partial" if overview_result.reason else "accepted"
                            task["reason"] = overview_result.reason
                            task["raw"] = None
                            _persist(checkpoints, key, state)
                            overview_path = _path(checkpoints, "overview", key, ".md")
                            atomic_write_text(overview_path, _accepted_overview(state))
                            record_first_inspectable()
                            if not overview_result.reason and not task.get("replay"):
                                break
                        else:
                            task["reason"] = overview_result.reason
                    task["raw"] = None
                    _persist(checkpoints, key, state)
                if needs_split:
                    break
                if task["status"] != "accepted":
                    task["status"] = "skipped"
                    task["reason"] = task.get("reason") or (
                        "planning_capacity_split_budget"
                        if max_attempts == 0
                        else "model_output_unusable"
                    )
                    _persist(checkpoints, key, state)
            if needs_split and _split_window(state, index, source, parsed, limits):
                _persist(checkpoints, key, state)
                continue
            if needs_split:
                for subtask in ("overview",):
                    task = state["tasks"].get(_task_id(window, subtask))
                    if task is not None and task["status"] not in {"accepted", "skipped"}:
                        task["status"] = "skipped"
                        task["reason"] = task.get("reason") or "planning_context_capacity"
                _persist(checkpoints, key, state)
            pages = [PagePlan.from_dict(row) for row in state["pages"]]
            references = _source_references(evidence, navigation, parsed, pages)
            state["pages"] = [page.to_dict() for page in pages]
            known_references = {row["key"] for row in state["external_references"]}
            for reference in references:
                if reference.key not in known_references:
                    state["external_references"].append(reference.to_dict())
                    known_references.add(reference.key)
            _persist(checkpoints, key, state)
            index += 1
    except ProcessingIncomplete as exc:
        if exc.reason not in {
            "request_budget_exhausted",
            "token_budget_exhausted",
            "time_budget_exhausted",
            "pages_request_reserved",
            "pages_tokens_reserved",
            "planning_recovery_budget",
        }:
            raise
        state["budget_limited"] = exc.reason
        for window in state["windows"]:
            for part in ("overview",):
                task = state["tasks"].setdefault(
                    _task_id(window, part), {"attempts": 0, "raw": None}
                )
                if task.get("status") != "accepted":
                    task.update(status="skipped", reason=exc.reason)
    from openkb.agent.document_global_planning import plan_global_pages

    try:
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
    pages = [PagePlan.from_dict(row) for row in state["pages"]]
    for window in state["windows"]:
        try:
            evidence = document_planning_support.read_target_evidence(
                kb_dir, source, parsed, window.get("evidence"), _target_ranges(window, parsed)
            )
        except (FileNotFoundError, KeyError, OSError, ValueError, TypeError):
            if mock_caller is None:
                continue
            evidence = document_planning_support.fallback_read_evidence(
                source, parsed, window["target_start"], window["target_end"]
            )
        for reference in _source_references(evidence, navigation, parsed, pages):
            existing = next(
                (row for row in state["external_references"] if row["key"] == reference.key), None
            )
            if existing is None:
                state["external_references"].append(reference.to_dict())
            else:
                existing["affected_pages"] = sorted(
                    set(existing["affected_pages"] + reference.affected_pages)
                )
    state["pages"] = [page.to_dict() for page in pages]
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
