"""Document planning: sequential windows, cumulative state, and checkpoints."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from copy import deepcopy
from typing import Any, Callable

from openkb.agent import document_planning_projection, document_planning_support, document_windowing
from openkb.agent.document_json_response import classify_json_response
from openkb.agent.document_plan import DocumentPlan
from openkb.agent.document_plan_feedback import RepairScopeError
from openkb.agent.document_plan_issues import PlanValidationError
from openkb.agent.document_plan_repair_state import V3PlanningRepairSession, planning_context
from openkb.agent.document_plan_salvage import salvage_candidate
from openkb.agent.document_planning_admission import admit_planning_windows, validate_navigation
from openkb.agent.document_planning_events import (
    emit_accepted_window,
    emit_planning_observation,
    emit_readmission,
    emit_split_target,
    frozen_prefix,
    planning_observation,
)
from openkb.agent.document_planning_ledger import DocumentPlanningLedger
from openkb.agent.document_planning_lifecycle import PlanningLifecycle
from openkb.agent.document_planning_partial import (
    prepare_partial_acceptance,
    report_partial_acceptance,
)
from openkb.agent.document_planning_result import PlanningResult
from openkb.agent.document_protocol import (
    PlanningProjectionRequired,
    decode_plan_response,
    plan_messages,
)
from openkb.agent.document_reference_review import review_candidate_references
from openkb.agent.document_window_receipts import accepted_window_receipt, window_receipt_id
from openkb.agent.evidence_units import JSON_FORMAT
from openkb.agent.evidence_wire import WireMessages
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
    retry_skipped: bool = False,
    plan_only: bool = False,
    mock_caller: Callable[..., Any] | None = None,
    return_result: bool = False,
) -> DocumentPlan | PlanningResult | None:
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
    validate_navigation(navigation, source, parsed)
    rules_rev = module_revision("openkb.agent.document_protocol")
    windowing_rev = module_revision("openkb.agent.document_windowing")
    planning_revisions = document_planning_support.planning_implementation_revisions()
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
    parser_conditions = document_planning_support.source_conditions(parsed)
    windows, planning_limits = admit_planning_windows(
        source,
        parsed,
        navigation,
        settings,
        limits,
        entity_types=entity_types,
        schema=schema,
        parser_conditions=parser_conditions,
    )
    record_document_totals(evidence_groups=len(windows))

    with closing(DocumentPlanningLedger(checkpoints, retained_key)) as ledger:
        lifecycle = PlanningLifecycle(
            ledger=ledger, checkpoints=checkpoints, retained_key=retained_key,
            wiki=wiki, source=source, parsed=parsed, navigation=navigation,
            settings=settings, schema=schema, rules_rev=rules_rev,
            windowing_rev=windowing_rev, planning_revisions=planning_revisions,
            recovery_contract=recovery_contract, entity_types=entity_types,
            planning_limits=planning_limits, plan_only=plan_only,
            return_result=return_result,
        )
        public_result = lifecycle.public_result

        try:
            # A caller that did not ask to Continue must never append a second
            # sequence of receipt rows to a retained plan of the same identity.
            metadata, cumulative_overview = lifecycle.initialize(
                reset=not resume and ledger.initialized
            )
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            sqlite3.Error,
        ):
            # A syntactically valid SQLite file can still contain a malformed
            # durable shape.  Treat it exactly like a cache miss rather than
            # letting recovery decoding escape to the caller.
            metadata, cumulative_overview = lifecycle.initialize(reset=True)
        if not windows:
            cumulative_overview.status = "complete"
        start_window = 0
        base_windows = deepcopy(windows)
        if resume:
            try:
                if not lifecycle.terminal_recovery_valid():
                    cumulative_overview = lifecycle.reset()
                elif not ledger.baseline_valid() or not ledger.verify_catalog(
                    wiki=wiki, source=source
                ):
                    cumulative_overview = lifecycle.reset()
                elif (recovered_progress := ledger.progress()) is None:
                    # Rebase even an interrupted pre-W ledger on the current catalogue.
                    cumulative_overview = lifecycle.reset()
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
                            retry = ledger.begin_retry(windows) if retry_skipped else None
                            if retry is not None:
                                windows, start_window = retry
                                checkpoints.save_recovery(
                                    retained_key, "plan", ledger.progress_preview()
                                )
                                on_event({
                                    "stage": "planning", "operation": "retry_omissions",
                                    "targets": len(windows) - start_window,
                                })
                            else:
                                return public_result(lifecycle.adopt_terminal(windows, on_event))
                        if start_window == len(windows):
                            return public_result(lifecycle.finalize(windows))
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
                        cumulative_overview = lifecycle.reset()
            except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error):
                windows, start_window = base_windows, 0
                cumulative_overview = lifecycle.reset()
        promoted_page_keys: set[str] = set()
        promoted_catalog_targets: set[str] = set()
        promoted_unresolved_keys: set[str] = set()

        def settle_content_failure(
            window: dict[str, Any], *, reason: str, attempts: int, predecessor: str
        ) -> None:
            lifecycle.settle_content_failure(
                window, windows=windows, window_index=w_idx,
                reason=reason, attempts=attempts, predecessor=predecessor,
                on_event=on_event,
            )
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
                "total_blocks": total_blocks,
                **(
                    {"ranges": planning_target_ranges}
                    if target_ranges is not None or target_was_projected
                    else {}
                ),
            }

            try:
                nav_hints = document_planning_support.select_navigation_hints(
                    navigation,
                    planning_target_ranges,
                    planning_limits,
                    evidence=evidence,
                    parsed=parsed,
                    model=settings["model"],
                )
            except ProcessingIncomplete as exc:
                if exc.reason != "planning_navigation_exceeds_request_budget":
                    raise
                children = document_windowing.reload_planning_target(source, parsed, window)
                if children:
                    windows[w_idx : w_idx + 1] = children
                    ledger.replace_schedule(windows, w_idx)
                    on_event({
                        "stage": "planning", "operation": "reload_planning_target",
                        "parts": len(children), "reason": exc.reason,
                    })
                    continue
                settle_content_failure(
                    window, reason="planning_context_capacity", attempts=0,
                    predecessor=ledger.state_digest(),
                )
                w_idx += 1
                continue
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
            ) -> WireMessages:
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
                    {
                        "stage": "planning",
                        "operation": "reload_planning_target",
                        "parts": len(children),
                    }
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
            decode_kwargs = document_planning_support.planning_decode_context(
                parsed,
                ledger,
                view,
                target_start=t_start,
                target_end=t_end,
                entity_types=entity_types,
                visible_targets=visible_targets,
                target_ranges=planning_target_ranges,
                evidence_ranges=evidence_ranges,
                prior_overview_ranges=cumulative_overview.ranges,
            )
            compile_context = planning_context(
                source,
                parsed,
                window,
                evidence,
                view,
                ledger,
                decode_kwargs,
                visible_targets,
                cumulative_overview.ranges,
            )
            predecessor = ledger.state_digest()
            raw_response = None
            output_exhausted = False
            accepted_checkpoint = None
            accepted_attempt = 0
            accepted_cached = False
            accepted_output_tokens = None
            request_details = None
            projection_required = False
            re_admit_after_expansion = False
            content_failure: str | None = None
            attempts_used = 0
            salvaged = None
            def promote_projection(required: PlanningProjectionRequired) -> None:
                document_planning_support.promote_planning_projection(
                    required,
                    promoted_page_keys,
                    promoted_catalog_targets,
                    promoted_unresolved_keys,
                    on_event,
                    w_idx + 1,
                )

            if mock_caller is not None:
                attempts_used = 1
                try:
                    raw_response = mock_caller(msgs, settings=settings)
                except ProcessingIncomplete as exc:
                    if exc.reason in {
                        "document_plan_invalid", "document_plan_response_invalid",
                        "document_plan_empty_response", "reference_check_invalid",
                        "reference_check_empty_response",
                    }:
                        content_failure = exc.reason
                    elif exc.reason != "output_budget_exhausted":
                        raise
                    else:
                        output_exhausted = True
            else:
                try:
                    planning_limits = document_windowing.for_window_attempts(
                        planning_limits, window, limits.max_attempts
                    )
                    repair = V3PlanningRepairSession(
                        msgs,
                        checkpoints,
                        window,
                        predecessor,
                        planning_limits,
                        decode_kwargs["block_chars"],
                        settings["model"],
                        compile_context,
                    )
                except ProcessingIncomplete as exc:
                    if exc.reason not in {
                        "document_plan_invalid", "document_plan_empty_response"
                    }:
                        raise
                    settle_content_failure(
                        window, reason=exc.reason, attempts=planning_limits.max_attempts,
                        predecessor=predecessor,
                    )
                    w_idx += 1
                    continue
                msgs = repair.messages
                for attempt in range(repair.next_attempt, planning_limits.max_attempts):
                    attempts_used = attempt + 1
                    request = json.loads(msgs[-1]["content"])
                    dependencies = {
                        "catalog_view": content_id(view.catalog_entries),
                        "schema": schema,
                        "window": window_receipt_id(window),
                        "attempt": attempt,
                    }
                    with checkpoints.request(
                        msgs[0]["content"], request, dependencies=dependencies
                    ) as key:
                        raw_text: Any = None
                        value = checkpoints.load(key)
                        cached = value is not None
                        try:
                            if value is None:
                                if repair.applied_candidate is not None:
                                    value = repair.applied_candidate
                                    raw_text = repair.replay_receipt()
                                else:
                                    if repair.pending_response is not None:
                                        raw_text = repair.pending_response
                                    else:
                                        marker = request_marker()
                                        try:
                                            raw_text = _llm_call(
                                                settings["model"],
                                                msgs,
                                                "planning",
                                                bundle=bundle,
                                                response_format=JSON_FORMAT,
                                                decode_response=False,
                                                **compilation_model_options(
                                                    settings, stage="planning"
                                                ),
                                            )
                                        finally:
                                            request_details = request_after(marker, "planning")
                                        repair.record_response(raw_text, key)
                                    response_kind = classify_json_response(raw_text)
                                    if response_kind == "empty_content":
                                        repair.empty_response(raw_text, attempt)
                                        msgs = repair.messages
                                        continue
                                    if response_kind == "length":
                                        raise ProcessingIncomplete(
                                            "output_budget_exhausted", "planning"
                                        )
                                    if response_kind != "content":
                                        raise ProcessingIncomplete(
                                            "document_plan_response_invalid", "planning"
                                        )
                                    if repair.phase:
                                        try:
                                            mapped = (
                                                raw_text
                                                if repair.phase == "syntax_repair"
                                                else msgs.decode_response(raw_text)
                                            )
                                        except (PlanValidationError, ValueError) as exc:
                                            raise RepairScopeError(
                                                "Malformed patch response"
                                            ) from exc
                                        value = repair.apply_response(mapped)
                                        if repair.phase == "syntax_repair":
                                            value = msgs.decode_response(value)
                                        repair.applied(value)
                                    else:
                                        try:
                                            value = msgs.decode_response(raw_text)
                                        except (PlanValidationError, ValueError):
                                            value = raw_text
                            result, feedback = repair.evaluate(value, compile_context, attempt)
                            if feedback is not None:
                                msgs = repair.messages
                                on_event(
                                    {
                                        "stage": "planning",
                                        "operation": "retry_invalid_response",
                                        "window": w_idx + 1,
                                        "cached": cached,
                                        "invalid_fields": [
                                            issue["path"] for issue in feedback["issues"]
                                        ],
                                    }
                                )
                                if attempt + 1 == planning_limits.max_attempts:
                                    raise ProcessingIncomplete("document_plan_invalid", "planning")
                                continue
                            assert result.delta is not None
                            decoded = result.delta
                            value = result.candidate
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
                            if exc.reason in {
                                "document_plan_invalid", "document_plan_response_invalid",
                                "document_plan_empty_response",
                            }:
                                content_failure = exc.reason
                                break
                            raise
                        except PlanningProjectionRequired as exc:
                            promote_projection(exc)
                            projection_required = True
                            break
                        except RepairScopeError as exc:
                            try:
                                repair.rejected(exc, attempt)
                            except ProcessingIncomplete as failure:
                                if failure.reason != "document_plan_invalid":
                                    raise
                                content_failure = failure.reason
                                break
                            msgs = repair.messages
                            continue
                        if not cached:
                            checkpoints.save(key, value, receipt=raw_text)
                        repair.accepted(value, decoded)
                        raw_response = value
                        accepted_checkpoint = key
                        accepted_attempt = attempt
                        accepted_cached = cached
                        accepted_output_tokens = checkpoints.dispatch_output_tokens(key)
                        break
            if re_admit_after_expansion:
                emit_readmission(on_event, w_idx, planning_limits.output_tokens)
                continue
            if content_failure is not None:
                if mock_caller is None:
                    salvaged = salvage_candidate(repair.candidate, compile_context, window)
                if salvaged is None:
                    settle_content_failure(
                        window, reason=content_failure,
                        attempts=attempts_used, predecessor=predecessor,
                    )
                    w_idx += 1
                    continue
                raw_response, decoded = salvaged.candidate, salvaged.delta
                accepted_attempt = attempts_used - 1
                content_failure = None
            if output_exhausted:
                children = document_windowing.split_planning_target(window, parsed)
                document_windowing.limit_split_attempts(
                    children, window, attempts_used, limits.max_attempts
                )
                if not children:
                    settle_content_failure(
                        window, reason="planning_output_budget_exhausted",
                        attempts=attempts_used, predecessor=predecessor,
                    )
                    w_idx += 1
                    continue
                windows[w_idx : w_idx + 1] = children
                ledger.replace_schedule(windows, w_idx)
                emit_split_target(
                    on_event, ledger, children, windows, w_idx, prefix=prefix,
                    candidate_count=len(view.page_register), window_started=window_started,
                    planning_started=planning_started, request=request_details,
                )
                continue
            if projection_required:
                continue
            if raw_response is None:
                settle_content_failure(
                    window, reason="response_empty", attempts=attempts_used,
                    predecessor=predecessor,
                )
                w_idx += 1
                continue

            if mock_caller is not None:
                try:
                    decoded = decode_plan_response(raw_response, **decode_kwargs)
                    document_planning_support.canonicalize_context_bases(decoded, evidence, parsed)
                except PlanningProjectionRequired as exc:
                    promote_projection(exc)
                    continue
                except (PlanValidationError, ValueError):
                    settle_content_failure(
                        window, reason="document_plan_invalid", attempts=attempts_used,
                        predecessor=predecessor,
                    )
                    w_idx += 1
                    continue

            try:
                reviewed = review_candidate_references(
                    raw_response,
                    decoded,
                    compile_context,
                    kb_dir=kb_dir,
                    source=source,
                    parsed=parsed,
                    navigation=navigation,
                    navigation_hints=nav_hints,
                    settings=settings,
                    limits=planning_limits,
                    checkpoints=checkpoints,
                    bundle=bundle,
                    predecessor=predecessor,
                    caller=_llm_call,
                    mock_caller=mock_caller,
                    on_event=on_event,
                    allow_coverage_gaps=salvaged is not None,
                    allow_empty_overview=(
                        salvaged is not None and salvaged.component == "overview"
                    ),
                )
            except ProcessingIncomplete as exc:
                if exc.reason not in {
                    "reference_check_invalid", "reference_check_empty_response",
                    "document_reference_check_invalid",
                }:
                    raise
                settle_content_failure(
                    window, reason=exc.reason, attempts=attempts_used,
                    predecessor=predecessor,
                )
                w_idx += 1
                continue
            raw_response, decoded = reviewed.candidate, reviewed.delta
            partial = prepare_partial_acceptance(
                ledger, checkpoints, window,
                original=repair.candidate if salvaged is not None else None,
                candidate=raw_response, delta=decoded, salvaged=salvaged,
                attempts=attempts_used,
                normalizations=repair.normalization if mock_caller is None else None,
            )

            receipt = accepted_window_receipt(
                window,
                raw_response,
                checkpoint=accepted_checkpoint,
                attempt=accepted_attempt,
                cached=accepted_cached,
                request=document_planning_support.accepted_request_reference(msgs),
                predecessor=predecessor,
                delta=content_id(decoded),
                dispatch_output_tokens=accepted_output_tokens,
                reference_check=reviewed.summary,
                normalization=partial.normalizations,
                salvage_proof=partial.proof_key,
            )
            cumulative_overview = ledger.apply_accepted(
                decoded,
                receipt,
                final_window=w_idx == len(windows) - 1,
                windows=windows,
                completed=w_idx + 1,
                partial_omission=partial.omission,
            )
            report_partial_acceptance(partial.omission, on_event, w_idx)

            emit_accepted_window(
                on_event,
                ledger,
                window,
                windows,
                receipt,
                prefix,
                page_register,
                open_references,
                w_idx,
                window_started,
                planning_started,
                request_details,
            )

            checkpoints.save_recovery(retained_key, "plan", ledger.progress_preview())
            promoted_page_keys.clear()
            promoted_catalog_targets.clear()
            promoted_unresolved_keys.clear()
            w_idx += 1

        final_plan = lifecycle.finalize(windows)
        with progress_scope("planning", len(windows)) as progress:
            progress.advance(len(windows))
        on_event(
            {
                "stage": "planning",
                "status": "accepted" if final_plan is not None else "empty",
                "pages": len(final_plan.pages) if final_plan is not None else 0,
                "unresolved": len(
                    [item for item in final_plan.unresolved if item.status == "open"]
                    if final_plan is not None else []
                ),
            }
        )
        return public_result(final_plan)
