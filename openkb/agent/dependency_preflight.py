"""Reject known impossible downstream inputs before spending on candidate pages."""

from dataclasses import replace

from openkb.compilation_report import collect_compile_report, report_content_omission
from openkb.processing import InputTooLarge, RequestLimits, processing_checkpoint


def known_omissions(parsed):
    from openkb.source_coverage import parsing_gaps

    with collect_compile_report() as report:
        omissions = list(report.omissions)
    omissions += [{"stage": "parsing", **row} for row in parsing_gaps(parsed)]
    omissions += [
        {
            "stage": "image_understanding",
            "block": block.id,
            "reason": "original_image_retained_understanding_pending",
            "assets": list(block.assets),
        }
        for block in parsed.blocks
        if block.assets
    ]
    return omissions


def source_fits(source, settings):
    """Measure complete serialized originals with the configured output reserve.

    Growing checkpoints bound work for impossible inputs. Candidate splitting
    cannot shrink this required source; it is never silently truncated.
    """
    from openkb.agent.evidence_dependencies import SYSTEM, dependency_payload
    from openkb.agent.evidence_units import JSON_FORMAT, messages
    from openkb.config import compilation_model_options

    limits = RequestLimits.from_config(settings)
    limits = replace(limits, context_tokens=limits.max_context_tokens or limits.context_tokens)
    options = {
        "response_format": JSON_FORMAT,
        **compilation_model_options(settings, verification=True),
    }
    selected = []

    def fits():
        payload = dependency_payload(
            {"stage": "dependencies", "source": selected, "omissions": [], "candidates": []}
        )
        try:
            limits.request(settings["model"], messages(SYSTEM, payload), options)
            return True
        except InputTooLarge:
            return False

    next_check = 8
    for row in source:
        processing_checkpoint("dependencies")
        selected.append(row)
        if len(selected) >= next_check:
            if not fits():
                return False
            next_check *= 2
    return fits()


def preflight_dependencies(reader, source, parsed, groups, facts, settings, on_event):
    omissions = known_omissions(parsed)
    if not groups or not omissions:
        return groups
    from openkb.agent.dependency_scope import review_scopes
    from openkb.agent.dependency_sources import OriginalRows, SourceSelection

    original = SourceSelection(OriginalRows(reader, source, parsed))
    candidates = [
        {"path": group["path"], "content": "", "facts": facts.routing_for_topics(group["members"])}
        for group in groups
    ]
    blocked = set()
    for required, _, affected in review_scopes(
        original, omissions, candidates, facts.routes.values(), groups
    ):
        if not source_fits(required, settings):
            blocked.update(row["path"] for row in affected)
    if blocked:
        report_content_omission(
            "generation", "dependency_context_exceeds_request_budget", sorted(blocked)
        )
        on_event(
            {"stage": "dependencies", "operation": "capacity_preflight", "omitted": len(blocked)}
        )
    return [group for group in groups if group["path"] not in blocked]


def omission_context(reader, source, parsed, omissions, group, facts, groups):
    if not omissions:
        return None
    from openkb.agent.dependency_sources import OriginalRows

    # The first factual review sees every known gap and originals inside its
    # own operation. Cross-scope relations are independently assessed by the
    # bounded dependency queue before any publication is authorized.
    own = {fact["scope"]["block_id"] for fact in facts.routing_for_topics(group["members"])}
    original = OriginalRows(reader, source, parsed)
    return {"omissions": omissions, "source": [original[bid] for bid in original if bid in own]}
