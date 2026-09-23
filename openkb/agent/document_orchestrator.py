"""Document planning: sequential windows, cumulative state, and checkpoints."""

from __future__ import annotations

import json
import sqlite3
import time
from copy import deepcopy
from typing import Any, Callable

from openkb.agent import document_planning_projection, document_planning_support, document_windowing
from openkb.agent.document_plan import DocumentPlan, to_dict
from openkb.agent.document_planning_events import (
    emit_planning_observation,
    frozen_prefix,
    planning_observation,
)
from openkb.agent.document_planning_ledger import DocumentPlanningLedger
from openkb.agent.document_protocol import (
    PlanningProjectionRequired,
    decode_plan_response,
    plan_messages,
)
from openkb.agent.document_window_receipts import accepted_window_receipt, window_receipt_id
from openkb.agent.evidence_units import JSON_FORMAT
from openkb.config import compilation_model_options, resolve_entity_types
from openkb.execution_measurement import record_document_totals, request_after, request_marker
from openkb.implementation import module_revision
from openkb.processing import (
    ProcessingIncomplete,
    RequestLimits,
    active_request_limits,
    processing_checkpoint,
)
from openkb.progress import progress_scope
from openkb.schema import get_agents_md
from openkb.sources import content_id


def plan_document(
    kb_dir: Any,
    workspace: Any,
    source: Any,
    parsed: Any,
    navigation: dict[str, Any] | None,
    settings: dict[str, Any],
    checkpoints: Any,
    *,
    bundle: Any = None,
    on_event: Callable[[dict[str, Any]], None] = lambda event: None,
    resume: bool = False,
    plan_only: bool = False,
    mock_caller: Callable[..., Any] | None = None,
) -> DocumentPlan:
    """Public entry point: compile source evidence into a coherent, durable DocumentPlan."""
    from openkb.agent.compiler import _llm_call

    processing_checkpoint("planning")
    on_event({"stage": "planning", "status": "started"})
    planning_started = time.monotonic()

    wiki = workspace.path / "wiki" if hasattr(workspace, "path") else workspace / "wiki"
    schema = get_agents_md(wiki)
    entity_types = resolve_entity_types(settings)
    total_blocks = len(parsed.blocks) if hasattr(parsed, "blocks") else 0
    limits = RequestLimits.from_config(settings)

    # Validate identity binding if navigation is present
    if navigation:
        if navigation.get("source_id") and navigation["source_id"] != source.source_id:
            raise ValueError("Navigation source_id mismatch")
        navigation_version = navigation.get("version_id", navigation.get("version"))
        navigation_parse = navigation.get("parse_id", navigation.get("parse"))
        if navigation_version and navigation_version != source.id:
            raise ValueError("Navigation version_id mismatch")
        if navigation_parse and navigation_parse != parsed.id:
            raise ValueError("Navigation parse_id mismatch")

    # Durable recovery identity
    rules_rev = module_revision("openkb.agent.document_protocol")
    windowing_rev = module_revision("openkb.agent.document_windowing")
    planning_revisions = {
        name: module_revision("openkb.agent." + name)
        for name in (
            "document_plan",
            "document_protocol",
            "document_range_validation",
            "document_orchestrator",
            "document_recovery",
            "document_windowing",
            "document_window_schedule",
            "document_window_receipts",
            "document_planning_events",
            "document_planning_ledger",
            "document_planning_ledger_integrity",
            "document_planning_ledger_views",
            "document_planning_projection",
            "document_planning_support",
        )
    }
    # Recovery is source/configuration-bound rather than keyed to a literal
    # current catalogue digest.  The ledger verifies the original catalogue
    # and permits only destinations the recovered plan published for this source.
    recovery_contract = document_planning_support.planning_contract(settings, limits)
    retained_key = document_planning_support.planning_identity(
        checkpoints,
        source=source,
        parsed=parsed,
        navigation=navigation,
        contract=recovery_contract,
        schema=schema,
        language=settings.get("language"),
        entity_types=entity_types,
        rules=rules_rev,
        windowing=windowing_rev,
        implementation=planning_revisions,
    )

    # Empty parser output is a source-only result; never construct [0, 0) W/T.
    windows: list[dict[str, Any]] = []
    planning_limits = document_planning_support.planning_admission_limits(limits)
    parser_conditions = document_planning_support.source_conditions(parsed)
    if total_blocks and not document_planning_support.no_readable_body(parsed):
        # Determine navigation windows
        windows = navigation.get("windows", []) if navigation else []
        if navigation and "windows" in navigation:
            from openkb.navigation_evidence import validate_windows

            validate_windows(source, parsed, windows)
        if not windows:
            # Single window covering entire document
            windows = [
                {
                    "evidence": None,
                    "target_start": 0,
                    "target_end": total_blocks,
                    "status": "complete",
                    "reason": "",
                    "target_tokens": 200000,
                }
            ]
        prompt_tokens = document_windowing.planning_prompt_tokens(
            source,
            parsed,
            settings,
            entity_types=entity_types,
            schema=schema,
            source_conditions=document_planning_projection.prompt_condition_template(
                parser_conditions
            ),
        )
        windows, planning_limits = document_windowing.bounded_windows(
            source, parsed, windows, planning_limits, prompt_tokens=prompt_tokens
        )
    record_document_totals(evidence_groups=len(windows))

    ledger = DocumentPlanningLedger(checkpoints, retained_key)

    def initialize_ledger(*, reset: bool = False) -> tuple[dict[str, Any], Any]:
        """Build a baseline or treat malformed durable state as a fresh plan."""

        if reset:
            ledger.reset()
        ledger.initialize(wiki=wiki, source=source, parsed=parsed)
        metadata = document_planning_support.planning_metadata(
            source=source,
            parsed=parsed,
            navigation=navigation,
            catalog_window="",
            catalog_targets=set(),
            catalog_entries=[],
            schema=schema,
            language=settings.get("language"),
            rules=rules_rev,
            windowing=windowing_rev,
            implementation=planning_revisions,
            recovery_key=retained_key,
            contract=recovery_contract,
            entity_types=entity_types,
            catalog_manifest=ledger.catalog_manifest(),
        )
        metadata["effective_planning_contract"] = document_planning_support.planning_contract(
            settings, planning_limits
        )
        ledger.bind_metadata(metadata)
        return metadata, ledger.overview()

    try:
        # A caller that did not ask to Continue must never append a second
        # sequence of receipt rows to a retained plan of the same identity.
        metadata, cumulative_overview = initialize_ledger(reset=not resume and ledger.initialized)
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
        # A syntactically valid SQLite file can still contain a malformed
        # durable shape.  Treat it exactly like a cache miss rather than
        # letting recovery decoding escape to the caller.
        metadata, cumulative_overview = initialize_ledger(reset=True)
    if not windows:
        cumulative_overview.status = "complete"
    start_window = 0
    base_windows = deepcopy(windows)

    def finalize() -> DocumentPlan:
        from openkb.agent.document_recovery import inherit_publication_state

        ledger.mark_accepted(windows)
        overview, pages, source_only, unresolved, resolutions = ledger.materialize()
        final = document_planning_support.final_document_plan(
            metadata={
                **metadata,
                **ledger.final_catalog_metadata(),
                "accepted_window_receipts": ledger.receipts(),
            },
            windows=windows,
            overview=overview,
            pages=pages,
            source_only=source_only,
            unresolved=unresolved,
            resolutions=resolutions,
            plan_only=plan_only,
            parsed=parsed,
            entity_types=entity_types,
            existing_targets=ledger.final_catalog_targets(),
        )
        # Terminal ledger materialization deliberately reconstructs planning
        # rows, so retain any prior completed proposal proof before replacing
        # this recovery record with the successor's plan state.
        inherit_publication_state(final, checkpoints.load_recovery(retained_key, "plan"))
        checkpoints.save_recovery(retained_key, "plan", to_dict(final))
        record_document_totals(planned_pages=len(final.pages))
        return final

    def reset_ledger() -> Any:
        ledger.reset()
        ledger.initialize(wiki=wiki, source=source, parsed=parsed)
        manifest = ledger.catalog_manifest()
        metadata.update(
            {
                "catalog_snapshot": manifest["snapshot"],
                "catalog_count": manifest["count"],
                "catalog_ledger": manifest["ledger"],
            }
        )
        ledger.bind_metadata(metadata)
        return ledger.overview()

    def terminal_recovery_valid() -> bool:
        """Validate the separately consumed terminal plan hand-off on Continue."""

        saved = checkpoints.load_recovery(retained_key, "plan")
        if saved is None or not isinstance(saved, dict) or "metadata" not in saved:
            return True
        try:
            from openkb.agent.document_plan import derive_page_states, from_dict, validate_plan

            restored = from_dict(saved)
            states = {page.key: page.state for page in restored.pages}
            validate_plan(restored, parsed, entity_types, ledger.final_catalog_targets())
            derive_page_states(restored.pages, restored.unresolved)
            return all(page.state == states[page.key] for page in restored.pages)
        except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error):
            return False

    if resume:
        try:
            if not terminal_recovery_valid():
                cumulative_overview = reset_ledger()
            elif not ledger.baseline_valid() or not ledger.verify_catalog(wiki=wiki, source=source):
                cumulative_overview = reset_ledger()
            elif (recovered_progress := ledger.progress()) is None:
                # Rebase even an interrupted pre-W ledger on the current catalogue.
                cumulative_overview = reset_ledger()
            else:
                status, recovered_windows, recovered_completed = recovered_progress
                if ledger.recovery_valid(
                    recovered_windows,
                    recovered_completed,
                    checkpoints.dispatch_output_tokens,
                    parsed=parsed,
                    source=source,
                    base_windows=base_windows,
                ):
                    windows, start_window = recovered_windows, recovered_completed
                    cumulative_overview = ledger.overview()
                    if status == "accepted":
                        restored = finalize()
                        receipt = ledger.receipt(start_window) or {}
                        on_event(
                            {
                                "stage": "planning",
                                "cached": True,
                                "retained": True,
                                "pages": ledger.page_count(),
                            }
                        )
                        if windows:
                            emit_planning_observation(
                                on_event,
                                planning_observation(
                                    "adopted",
                                    windows[-1],
                                    windows,
                                    completed=len(windows),
                                    carry_pages=ledger.page_count(),
                                    carry_unresolved=ledger.open_unresolved_count(),
                                    pages=ledger.page_count(),
                                    unresolved=ledger.open_unresolved_count(),
                                    checkpoint=receipt.get("checkpoint"),
                                    result=receipt.get("result"),
                                    attempt=receipt.get("attempt", 0),
                                    cached=True,
                                ),
                            )
                        return restored
                    if start_window == len(windows):
                        return finalize()
                    on_event(
                        {
                            "stage": "planning",
                            "operation": "resume",
                            "completed_windows": start_window,
                            "total_windows": len(windows),
                        }
                    )
                    emit_planning_observation(
                        on_event,
                        planning_observation(
                            "resume",
                            windows[start_window],
                            windows,
                            completed=start_window,
                            carry_pages=ledger.page_count(),
                            carry_unresolved=ledger.open_unresolved_count(),
                            pages=ledger.page_count(),
                            unresolved=ledger.open_unresolved_count(),
                        ),
                    )
                else:
                    # Corrupt mutable state is a cache miss.  The validated model
                    # checkpoints remain reusable when the fresh planner asks them.
                    cumulative_overview = reset_ledger()
        except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error):
            windows, start_window = base_windows, 0
            cumulative_overview = reset_ledger()

    promoted_page_keys: set[str] = set()
    promoted_catalog_targets: set[str] = set()
    promoted_unresolved_keys: set[str] = set()
    w_idx = start_window
    while w_idx < len(windows):
        window = windows[w_idx]
        window_started = time.monotonic()
        processing_checkpoint("planning")
        t_start = window["target_start"]
        t_end = window["target_end"]

        # Read frozen evidence
        frozen_descriptor = window.get("evidence")
        desc = frozen_descriptor
        target_ranges = window.get("target_ranges")
        frozen_ranges = window.get("frozen_ranges")
        if desc is None:
            # A document without navigation still needs the same durable reader
            # used for saved navigation groups.  Parsed manifests carry only
            # block metadata, not source text, so using the fixture fallback here
            # would send blank evidence to a real planner.
            if frozen_ranges or target_ranges:
                desc = {"id": window_receipt_id(window)}
            else:
                from openkb.navigation_evidence import evidence_descriptor

                desc = evidence_descriptor(source, parsed, t_start, t_end)
        try:
            evidence = document_planning_support.read_target_evidence(
                kb_dir,
                source,
                parsed,
                desc,
                None if frozen_descriptor is not None else frozen_ranges or target_ranges,
            )
        except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
            if mock_caller is None:
                raise ProcessingIncomplete("planned_evidence_unavailable", "planning") from exc
            fallback_ranges = frozen_ranges or target_ranges
            if frozen_descriptor is None and fallback_ranges:
                evidence = document_planning_support.fallback_target_evidence(
                    source, parsed, fallback_ranges
                )
            else:
                evidence = document_planning_support.fallback_read_evidence(
                    source, parsed, desc.get("start", t_start), desc.get("end", t_end)
                )
        prefix = frozen_prefix(evidence)

        relevant = ledger.relevant_keys(
            evidence,
            t_start,
            t_end,
            page_keys=promoted_page_keys,
            catalog_targets=promoted_catalog_targets,
            unresolved_keys=promoted_unresolved_keys,
        )

        original_target_ranges = target_ranges or [[t_start, t_end]]
        planning_target_ranges, target_was_projected = (
            document_planning_support.exclude_attachment_ranges(parsed, original_target_ranges)
        )
        target_t = {
            "target_start": t_start,
            "target_end": t_end,
            **(
                {"ranges": planning_target_ranges}
                if target_ranges is not None or target_was_projected
                else {}
            ),
        }

        # Filter navigation hints for this window
        nav_hints = [
            {k: node.get(k) for k in ("title", "summary", "summary_origin") if k in node}
            for node in (navigation.get("nodes", []) if navigation else [])
            if t_start <= node.get("start", 0) < t_end
        ][:12]
        page_register, open_references = ledger.projected_carry(relevant, t_start, t_end)
        terms = {
            word.strip(".,:;()[]{}!?'\"").lower()
            for block in evidence["blocks"]
            for word in str(block.get("text", "")).split()
            if len(word.strip(".,:;()[]{}!?'\"")) >= 3
        }
        ranked_catalog = ledger.ranked_catalog(terms, relevant["catalog_targets"])
        conditions = document_planning_projection.project_source_conditions(
            parser_conditions, parsed, planning_target_ranges
        )

        def assemble(
            visible_pages: list[dict[str, Any]],
            visible_unresolved: list[dict[str, Any]],
            visible_catalog: list[tuple[str, str]],
        ) -> list[dict[str, Any]]:
            return plan_messages(
                evidence=evidence,
                carry_s={
                    "overview": cumulative_overview.text,
                    "page_register": visible_pages,
                    "open_references": visible_unresolved,
                },
                target_t=target_t,
                navigation_hints=nav_hints,
                catalog_window="\n".join(brief for _, brief in visible_catalog if brief)
                or "(none relevant)",
                entity_types=entity_types,
                schema=schema,
                language=settings.get("language", ""),
                catalog_targets=[target for target, _ in visible_catalog],
                source_conditions=conditions,
            )

        try:
            view = document_windowing.project_planning_view(
                model=settings["model"],
                limits=planning_limits,
                page_register=page_register,
                open_references=open_references,
                catalog_entries=ranked_catalog,
                required_page_keys=relevant["page_keys"],
                required_unresolved_keys=relevant["unresolved_keys"],
                required_catalog_targets=relevant["catalog_targets"],
                assemble=assemble,
            )
        except ProcessingIncomplete as exc:
            if exc.reason not in {
                "planning_context_exceeds_request_budget",
                "planning_relevant_state_exceeds_request_budget",
            }:
                raise
            children = document_windowing.reload_planning_target(source, parsed, window)
            if not children:
                raise
            windows[w_idx : w_idx + 1] = children
            ledger.replace_schedule(windows, w_idx)
            on_event(
                {"stage": "planning", "operation": "reload_planning_target", "parts": len(children)}
            )
            continue
        msgs = view.messages
        visible_targets = {target for target, _ in view.catalog_entries}
        raw_evidence_ranges = (
            [[desc["start"], desc["end"]]]
            if frozen_descriptor is not None and isinstance(desc, dict)
            else frozen_ranges
            or [[block["order"], block["order"] + 1] for block in evidence["blocks"]]
        )
        evidence_ranges, _ = document_planning_support.exclude_attachment_ranges(
            parsed, raw_evidence_ranges
        )
        decode_kwargs = {
            "target_start": t_start,
            "target_end": t_end,
            "total_blocks": total_blocks,
            "allowed_entity_types": entity_types,
            "existing_targets": visible_targets,
            "reserved_targets": ledger.catalog_targets(),
            "known_page_names": ledger.page_names(),
            "known_page_name_keys": ledger.page_names(),
            "known_page_keys": ledger.page_keys(),
            "carry_pages": view.page_register,
            "open_unresolved": view.open_references,
            "known_unresolved_keys": ledger.unresolved_keys(),
            "known_open_unresolved_keys": ledger.unresolved_keys(status="open"),
            "block_chars": [block.chars for block in parsed.blocks],
            "ignored_blocks": {
                index
                for index, block in enumerate(parsed.blocks)
                if "attachment" in getattr(block, "location", {})
            },
            "target_ranges": planning_target_ranges,
            "evidence_ranges": evidence_ranges,
            "prior_overview_ranges": cumulative_overview.ranges,
        }
        predecessor = ledger.state_digest()
        request_reference = content_id(
            {"system": msgs[0]["content"], "payload": json.loads(msgs[-1]["content"])}
        )

        raw_response = None
        output_exhausted = False
        accepted_checkpoint = None
        accepted_attempt = 0
        accepted_cached = False
        accepted_output_tokens = None
        request_details = None
        projection_required = False
        re_admit_after_expansion = False

        def retry_invalid(key: str, attempt: int, cached: bool) -> None:
            on_event(
                {
                    "stage": "planning",
                    "operation": "retry_invalid_response",
                    "window": w_idx + 1,
                    "cached": cached,
                }
            )
            emit_planning_observation(
                on_event,
                planning_observation(
                    "retry",
                    window,
                    windows,
                    completed=w_idx,
                    carry_pages=len(page_register),
                    carry_unresolved=len(open_references),
                    pages=ledger.page_count(),
                    unresolved=ledger.open_unresolved_count(),
                    checkpoint=key,
                    attempt=attempt,
                    cached=cached,
                    reason="invalid_response",
                    prefix=prefix,
                    candidate_count=len(view.page_register),
                    elapsed_seconds=time.monotonic() - window_started,
                    frozen_last_use_seconds=time.monotonic() - planning_started,
                    request=request_details,
                ),
            )

        def promote_projection(required: PlanningProjectionRequired) -> None:
            if required.page_key:
                promoted_page_keys.add(required.page_key)
            if required.catalog_target:
                promoted_catalog_targets.add(required.catalog_target)
            if required.unresolved_key:
                promoted_unresolved_keys.add(required.unresolved_key)
            on_event(
                {
                    "stage": "planning",
                    "operation": "promote_planning_ledger",
                    "window": w_idx + 1,
                }
            )

        if mock_caller is not None:
            try:
                raw_response = mock_caller(msgs, settings=settings)
            except ProcessingIncomplete as exc:
                if exc.reason != "output_budget_exhausted":
                    raise
                output_exhausted = True
        else:
            request = json.loads(msgs[-1]["content"])
            # A bad response is never accepted into the normal cache: a bounded
            # retry gets a distinct suffix identity while preserving the same W.
            for attempt in range(limits.max_attempts):
                dependencies = {
                    "catalog_view": content_id(view.catalog_entries),
                    "schema": schema,
                    "window": window_receipt_id(window),
                    "attempt": attempt,
                }
                with checkpoints.request(
                    msgs[0]["content"], request, dependencies=dependencies
                ) as key:
                    raw_text = None
                    value = checkpoints.load(key)
                    cached = value is not None
                    try:
                        if value is None:
                            marker = request_marker()
                            try:
                                raw_text = _llm_call(
                                    settings["model"],
                                    msgs,
                                    "planning",
                                    bundle=bundle,
                                    response_format=JSON_FORMAT,
                                    **compilation_model_options(settings, stage="planning"),
                                )
                            finally:
                                request_details = request_after(marker, "planning")
                            value = (
                                msgs.decode_response(raw_text)
                                if hasattr(msgs, "decode_response")
                                else json.loads(raw_text)
                            )
                        decoded = decode_plan_response(value, **decode_kwargs)
                        document_planning_support.canonicalize_context_bases(
                            decoded, evidence, parsed
                        )
                    except ProcessingIncomplete as exc:
                        if exc.reason == "output_budget_exhausted":
                            output_exhausted = True
                            break
                        if exc.reason == "input_budget_exceeded":
                            # ``ExecutionBudget`` can grow a completion
                            # reservation after a length finish.  The second
                            # send is deliberately rejected locally when the
                            # old W/S/T would exceed that new shared budget.
                            # Re-enter projection instead of terminating a
                            # request that was never sent at the larger cap.
                            expanded = active_request_limits()
                            if expanded is not None and expanded != planning_limits:
                                planning_limits = expanded
                                re_admit_after_expansion = True
                                break
                        if exc.reason != "evidence_output_invalid":
                            raise
                        if attempt + 1 == limits.max_attempts:
                            raise ProcessingIncomplete(
                                "document_plan_invalid", "planning"
                            ) from None
                        retry_invalid(key, attempt, cached)
                        continue
                    except PlanningProjectionRequired as exc:
                        promote_projection(exc)
                        projection_required = True
                        break
                    except (AttributeError, TypeError, ValueError):
                        if attempt + 1 == limits.max_attempts:
                            raise ProcessingIncomplete(
                                "document_plan_invalid", "planning"
                            ) from None
                        retry_invalid(key, attempt, cached)
                        continue
                    if not cached:
                        checkpoints.save(key, value, receipt=raw_text)
                    raw_response = value
                    accepted_checkpoint = key
                    accepted_attempt = attempt
                    accepted_cached = cached
                    accepted_output_tokens = checkpoints.dispatch_output_tokens(key)
                    break
        if re_admit_after_expansion:
            on_event(
                {
                    "stage": "planning",
                    "operation": "re_admit_planning_request",
                    "window": w_idx + 1,
                    "output_tokens": planning_limits.output_tokens,
                }
            )
            continue
        if output_exhausted:
            children = document_windowing.split_planning_target(window, parsed)
            if not children:
                raise ProcessingIncomplete("planning_output_budget_exhausted", "planning")
            windows[w_idx : w_idx + 1] = children
            ledger.replace_schedule(windows, w_idx)
            on_event(
                {
                    "stage": "planning",
                    "operation": "split_planning_target",
                    "window": w_idx + 1,
                    "parts": len(children),
                    "frozen_evidence": children[0]["frozen_evidence_id"],
                }
            )
            emit_planning_observation(
                on_event,
                planning_observation(
                    "split",
                    children[0],
                    windows,
                    completed=w_idx,
                    carry_pages=ledger.page_count(),
                    carry_unresolved=ledger.open_unresolved_count(),
                    pages=ledger.page_count(),
                    unresolved=ledger.open_unresolved_count(),
                    reason="output_budget_exhausted",
                    parts=len(children),
                    prefix=prefix,
                    candidate_count=len(view.page_register),
                    elapsed_seconds=time.monotonic() - window_started,
                    frozen_last_use_seconds=time.monotonic() - planning_started,
                    request=request_details,
                ),
            )
            continue
        if projection_required:
            continue
        assert raw_response is not None

        try:
            decoded = decode_plan_response(raw_response, **decode_kwargs)
            document_planning_support.canonicalize_context_bases(decoded, evidence, parsed)
        except PlanningProjectionRequired as exc:
            promote_projection(exc)
            continue

        receipt = accepted_window_receipt(
            window,
            raw_response,
            checkpoint=accepted_checkpoint,
            attempt=accepted_attempt,
            cached=accepted_cached,
            request=request_reference,
            predecessor=predecessor,
            delta=content_id(decoded),
            dispatch_output_tokens=accepted_output_tokens,
        )
        cumulative_overview = ledger.apply_accepted(
            decoded,
            receipt,
            final_window=w_idx == len(windows) - 1,
            windows=windows,
            completed=w_idx + 1,
        )

        on_event(
            {
                "stage": "planning",
                "window": w_idx + 1,
                "total_windows": len(windows),
                "pages": ledger.page_count(),
                "unresolved": ledger.open_unresolved_count(),
            }
        )
        emit_planning_observation(
            on_event,
            planning_observation(
                "accepted",
                window,
                windows,
                completed=w_idx + 1,
                carry_pages=len(page_register),
                carry_unresolved=len(open_references),
                pages=ledger.page_count(),
                unresolved=ledger.open_unresolved_count(),
                checkpoint=receipt["checkpoint"],
                result=receipt["result"],
                attempt=receipt["attempt"],
                cached=receipt["cached"],
                prefix=prefix,
                candidate_count=ledger.page_count(),
                elapsed_seconds=time.monotonic() - window_started,
                frozen_last_use_seconds=time.monotonic() - planning_started,
                request=request_details,
            ),
        )

        checkpoints.save_recovery(retained_key, "plan", ledger.progress_preview())
        promoted_page_keys.clear()
        promoted_catalog_targets.clear()
        promoted_unresolved_keys.clear()
        w_idx += 1

    final_plan = finalize()
    with progress_scope("planning", len(windows)) as progress:
        progress.advance(len(windows))
    on_event(
        {
            "stage": "planning",
            "status": "accepted",
            "pages": len(final_plan.pages),
            "unresolved": len([item for item in final_plan.unresolved if item.status == "open"]),
        }
    )
    return final_plan
