"""Independent Markdown overview and page planning over persisted source windows."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from openkb import frontmatter
from openkb.agent import document_planning_support, document_windowing
from openkb.agent.document_plan import PagePlan, RangeValue
from openkb.agent.document_plan_annotations import ExternalReference
from openkb.agent.document_planning_admission import admit_planning_windows, validate_navigation
from openkb.agent.document_planning_candidates import (
    retain_split_candidates,
    valid_split_candidate_targets,
)
from openkb.agent.document_planning_locations import section_contribution
from openkb.agent.document_planning_report import (
    _finalize,
    _overview_text,
    _path,
    _target_ranges,
    _task_id,
    ordered_fragments,
)
from openkb.agent.document_planning_response import accept_overview, accept_pages
from openkb.agent.document_planning_result import PlanningResult
from openkb.agent.document_protocol import plan_messages
from openkb.agent.document_window_receipts import window_receipt_id
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


def _state(
    checkpoints: Any, key: str, windows: list[dict[str, Any]], resume: bool
) -> dict[str, Any]:
    previous = checkpoints.load_recovery(key, "markdown_plan") if resume else None
    if previous is not None:
        if not _valid_state(previous, windows):
            raise ProcessingIncomplete("planning_recovery_invalid", "planning")
        previous.setdefault("filtered", [])
        return previous
    return {
        "protocol": "document-planning-markdown-v1",
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


def _valid_state(value: Any, original_windows: list[dict[str, Any]]) -> bool:
    if not isinstance(value, dict) or value.get("protocol") != "document-planning-markdown-v1":
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
    if not valid_split_candidate_targets(value, original_windows):
        return False
    return all(
        isinstance(key, str)
        and isinstance(task, dict)
        and task.get("status") in {"pending", "accepted", "partial", "skipped"}
        and type(task.get("attempts")) is int
        and task["attempts"] >= 0
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


def _accepted_overview(state: dict[str, Any]) -> str:
    return _overview_text(ordered_fragments(state))


def _split_window(
    state: dict[str, Any], index: int, source: Any, parsed: Any, limits: RequestLimits
) -> bool:
    """Divide the remaining A/B attempts without replaying accepted parent work."""
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
        cap = min(
            limits.max_attempts,
            window.get("attempt_limits", {}).get(
                subtask, window.get("attempt_limit", limits.max_attempts)
            ),
        )
        remaining = max(0, cap - parent_task.get("attempts", 0))
        share, extra = divmod(remaining, len(children))
        for child_index, child in enumerate(children):
            child.setdefault("attempt_limits", {})[subtask] = share + (child_index < extra)
            if parent_task.get("status") in {"accepted", "skipped"}:
                state["tasks"][_task_id(child, subtask)] = {
                    "status": parent_task["status"],
                    "attempts": 0,
                    "raw": None,
                    "reason": parent_task.get("reason"),
                }
    retain_split_candidates(state, window, _target_ranges(window, parsed))
    state["windows"][index : index + 1] = children
    return True


def _persist(checkpoints: Any, key: str, state: dict[str, Any]) -> None:
    checkpoints.save_recovery(key, "markdown_plan", state)


def _raw_value(raw: Any) -> dict[str, str]:
    return {
        "content": str(raw or ""),
        "finish_reason": str(getattr(raw, "finish_reason", "stop") or "stop"),
    }


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
    """Run bounded A/B work per window and checkpoint each accepted component."""
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
        for task in state["tasks"].values():
            if task.get("status") == "skipped":
                task.update(status="pending", attempts=0, reason=None, raw=None)
        state["rejected"] = []
        _persist(checkpoints, key, state)
    index = 0
    while index < len(state["windows"]):
        window = state["windows"][index]
        processing_checkpoint("planning")
        desc = window.get("evidence")
        if desc is None:
            from openkb.navigation_evidence import evidence_descriptor

            desc = evidence_descriptor(source, parsed, window["target_start"], window["target_end"])
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
            for subtask in ("overview", "pages"):
                state["tasks"][_task_id(window, subtask)] = {
                    "status": "skipped",
                    "attempts": 0,
                    "raw": None,
                    "reason": "planning_navigation_exceeds_request_budget",
                }
            _persist(checkpoints, key, state)
        target = _target_ranges(window, parsed)
        locator = _navigation_rows(hints, navigation, parsed, target=target, evidence=evidence)
        selected_catalog = _catalog_window(catalog_entries, evidence, hints)
        needs_split = False
        for subtask in ("overview", "pages"):
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
            max_attempts = min(
                limits.max_attempts,
                window.get("attempt_limits", {}).get(
                    subtask, window.get("attempt_limit", limits.max_attempts)
                ),
            )
            while task["attempts"] < max_attempts or task.get("raw") is not None:
                processing_checkpoint("planning")
                recovery = task.get("reason") or ""
                if recovery and subtask == "pages":
                    from openkb.agent.document_planning_candidates import pending_candidates

                    rejected = pending_candidates(state, task_key)[-8:]
                    details = [f"{row['reason']}: {row['candidate'][:160]}" for row in rejected]
                    alternatives = [
                        f"{row['section_key']} {' > '.join(row.get('heading_path', []))}"
                        for row in hints[:30]
                    ]
                    recovery = "\n".join(
                        [
                            recovery,
                            "未接受候选（可重新组织，仅补未完成部分）：",
                            *details,
                            "可用章节路径：",
                            *alternatives,
                        ]
                    )
                overview = _accepted_overview(state)
                displayed_targets = [row[0] for row in selected_catalog]
                catalog_text = "\n".join(
                    f"- {path} | {title}" + (f" — {brief}" if brief else "")
                    for path, title, brief in selected_catalog
                )
                messages = plan_messages(
                    evidence,
                    {
                        "overview": overview[-4000:],
                        "pages": [
                            {"title": p["title"], "kind": p["kind"]} for p in state["pages"][-40:]
                        ],
                    },
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
                    settings.get("language", ""),
                    displayed_targets,
                    source_conditions=source_conditions,
                    subtask=subtask,
                    recovery=recovery,
                )
                if task.get("raw") is None:
                    task["attempts"] += 1
                    state["attempts"] += 1
                    if task["attempts"] > 1:
                        state["recovery_requests"] = state.get("recovery_requests", 0) + 1
                    _persist(checkpoints, key, state)
                    marker = request_marker()
                    try:
                        raw = _call(
                            messages, settings, planning_limits, bundle, mock_caller, subtask
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
                        planning_limits = planning_limits.expanded(reason="output_budget_exhausted")
                        _persist(checkpoints, key, state)
                        continue
                    finally:
                        dispatched = max(0, request_marker() - marker)
                        state.setdefault("request_usage", []).extend(request_usage_since(marker))
                        if dispatched > 1:
                            task["attempts"] += dispatched - 1
                            state["attempts"] += dispatched - 1
                            state["recovery_requests"] = (
                                state.get("recovery_requests", 0) + dispatched - 1
                            )
                            _persist(checkpoints, key, state)
                    task["raw"] = _raw_value(raw)
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
                        fragment_id = window_receipt_id(window)
                        state["fragments"][fragment_id] = _overview_text(
                            [state["fragments"].get(fragment_id, ""), overview_result.text]
                        ).strip()
                        task["status"] = "partial" if overview_result.reason else "accepted"
                        task["reason"] = overview_result.reason
                        task["raw"] = None
                        _persist(checkpoints, key, state)
                        overview_path = _path(checkpoints, "overview", key, ".md")
                        atomic_write_text(overview_path, _accepted_overview(state))
                        record_first_inspectable()
                        if not overview_result.reason:
                            break
                    else:
                        task["reason"] = overview_result.reason
                else:
                    accepted_pages = [PagePlan.from_dict(item) for item in state["pages"]]
                    pages_result = accept_pages(
                        response,
                        navigation=locator,
                        target=target,
                        parsed=parsed,
                        entity_types=entity_types,
                        existing_targets=existing_targets,
                        allowed_update_targets=set(displayed_targets),
                        catalog_titles={path: title for path, title, _ in catalog_entries},
                        accepted=accepted_pages,
                        default_entity_type=settings.get("default_entity_type"),
                        evidence=json.loads(messages[-1]["content"])["evidence"],
                    )
                    state["pages"] = [
                        page.to_dict() for page in accepted_pages + pages_result.pages
                    ]
                    from openkb.agent.document_planning_candidates import record_candidate_attempt

                    has_pending = record_candidate_attempt(
                        state, task_key, task["attempts"], pages_result
                    )
                    if pages_result.usable:
                        task["no_pages_recommended"] = pages_result.no_pages
                        task["status"] = (
                            "accepted"
                            if not has_pending and not pages_result.truncated
                            else "partial"
                        )
                        task["reason"] = (
                            "pages_truncated"
                            if pages_result.truncated
                            else "page_candidates_rejected"
                            if has_pending
                            else None
                        )
                    else:
                        task["reason"] = (
                            "pages_truncated"
                            if pages_result.truncated
                            else "page_candidates_rejected"
                            if has_pending
                            else task.get("reason") or "pages_unparseable"
                        )
                    task["raw"] = None
                    _persist(checkpoints, key, state)
                    if pages_result.pages:
                        record_first_inspectable()
                    if task["status"] == "accepted":
                        break
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
            for subtask in ("overview", "pages"):
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
