"""A bounded pages-prompt comparison using saved inputs and production dispatch.

Saved prompts are compared with the production builder using an explicit input-diff
allowlist. The normal Step 3 run retains the production recovery state machine.
"""

from __future__ import annotations

import json
from copy import deepcopy

from openkb.agent.document_markdown_prompts import planning_rules
from openkb.agent.evidence_wire import WireMessages
from openkb.agent.source_protocol import request_payload
from openkb.sources import content_id


def comparison_messages(saved):
    """Replay a known initial global-pages request, never its displayed SDK options."""
    rows = saved["messages"]
    payload = request_payload(rows)
    if (
        payload.get("plan_protocol") != "document-planning-markdown-v1"
        or payload.get("subtask") != "pages"
        or payload.get("target", {}).get("kind") != "global_pages"
        or payload.get("recovery")
        or payload["carry"].get("pages")
        or payload["carry"].get("deferred_suggestions")
        or not payload["carry"].get("overview", {}).get("text")
    ):
        raise ValueError("Comparison requires an initial global pages request with an overview")
    candidate = deepcopy(rows)
    # Preserve the exact prefix bytes, including original serialization, not just JSON equality.
    original_rules = json.dumps(payload["task_rules"], ensure_ascii=False)
    old_suffix = ',"task_rules":' + original_rules + "}"
    if not rows[-1]["content"].endswith(old_suffix):
        raise ValueError("Comparison requires task_rules as the final serialized field")
    rules = planning_rules(
        "pages", "global_pages", payload["planning_context"].get("navigation_style", "")
    )
    candidate[-1]["content"] = (
        rows[-1]["content"][: -len(old_suffix)]
        + ',"task_rules":'
        + json.dumps(rules, ensure_ascii=False)
        + "}"
    )
    identities = {original: alias for alias, original in saved["inverse"].items()}
    return WireMessages(deepcopy(rows), identities), WireMessages(candidate, identities)


def runtime_candidate(saved, kb, source, parsed, navigation, settings):
    """Rebuild B through the same fact collector, freeze and message builders as production."""
    from openkb.agent.document_global_context import freeze_context, messages_for
    from openkb.agent.document_planning_runtime import read_planning_inputs
    from openkb.processing import RequestLimits

    payload = request_payload(saved["messages"])
    common = payload["planning_context"]["common_inputs"]
    limits = RequestLimits.from_config(settings)
    inputs = read_planning_inputs(kb, kb / "wiki", source, parsed, settings, limits)
    state = {
        **inputs,
        "pages": [],
        "deferred_suggestions": [],
        "suggestion_annotations": {},
        "overview_snapshot": dict(payload["carry"]["overview"]),
        "windows": [],
        "retained_fragments": [],
        "fragments": {},
        "tasks": {},
    }
    snapshot = freeze_context(
        state,
        navigation,
        source,
        parsed,
        inputs["catalog"],
        settings,
        limits,
        common["entity_types"],
        common["schema"],
        common["source_conditions"],
    )
    candidate = messages_for(
        snapshot,
        snapshot["nodes"],
        state,
        source,
        parsed,
        settings,
        limits,
        common["entity_types"],
        common["schema"],
        common["source_conditions"],
    )
    return candidate


def input_differences(baseline, candidate):
    """Reject accidental experiment changes, including overview, source, and navigation."""

    def logical(messages):
        from openkb.agent.evidence_wire import _map

        return _map(request_payload(messages), messages.inverse)

    before, after = logical(baseline), logical(candidate)
    allowed = (
        "task_rules",
        "planning_context.runtime",
        "planning_context.catalog",
        "target.snapshot_id",
        "carry.selection_counts",
        "carry.suggested_new_concepts_remaining",
    )
    differences = []

    def visit(a, b, path=""):
        if a == b:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                visit(a.get(key), b.get(key), path + "." + key if path else key)
        else:
            differences.append(path)

    visit(before, after)
    unexpected = [
        path
        for path in differences
        if not any(path == key or path.startswith(key + ".") for key in allowed)
    ]
    if unexpected:
        raise ValueError("Uncontrolled comparison input changes: " + ", ".join(unexpected))
    return differences


def compare_pages(path, audit, profile, settings, kb, source, parsed, navigation):
    """Two fixed-overview pairs A-B / B-A; no adaptive retries to improve a score."""
    from openkb.agent.document_global_context import navigation_rows
    from openkb.agent.document_markdown_planner import _call
    from openkb.agent.document_page_resolution import prepare_page
    from openkb.agent.document_planning_bindings import capture_request
    from openkb.agent.document_planning_response import accept_pages
    from openkb.agent.document_planning_runtime import preparation_max_chars
    from openkb.pageindex_store import indexed_reader
    from openkb.processing import RequestLimits, processing_scope
    from openkb.sources import SourceStore

    saved = json.loads(path.read_text())
    baseline, _ = comparison_messages(saved)
    payload = request_payload(baseline)
    context = payload["planning_context"]
    if context["navigation_id"] != content_id(navigation.get("nodes", [])):
        raise ValueError("Comparison navigation differs from the saved request")
    common = context["common_inputs"]
    if context["catalog"] or common["existing_targets"] or common["existing_pages"]:
        raise ValueError("Comparison currently requires an empty existing-page catalogue")
    candidate = runtime_candidate(saved, kb, source, parsed, navigation, settings)
    differences = input_differences(baseline, candidate)
    limits = RequestLimits.from_config(settings)
    maximum = preparation_max_chars(limits)
    audit.write(
        "comparison-input.json",
        {
            "saved_request": path,
            "request_differences": differences,
            "overview_sha256": content_id(payload["carry"]["overview"]),
            "max_chars": maximum,
            "order": ["baseline-1", "candidate-1", "candidate-2", "baseline-2"],
            "preparation": "deterministic reading only; page_sources recovery evaluated separately",
        },
    )
    try:
        for label, messages in (
            ("baseline-1", baseline),
            ("candidate-1", candidate),
            ("candidate-2", candidate),
            ("baseline-2", baseline),
        ):
            audit.phase = "comparison_" + label
            binding = capture_request(messages, source, parsed, payload["target"], SourceStore(kb))
            with processing_scope(settings):
                raw = _call(messages, settings, limits, profile.bundle, None, "pages")
                accepted = accept_pages(
                    raw,
                    navigation=navigation_rows(navigation, parsed),
                    target=[],
                    parsed=parsed,
                    entity_types=common["entity_types"],
                    existing_targets=set(),
                    default_entity_type=settings.get("default_entity_type"),
                    source_identity=source.source_id,
                    request_binding=binding,
                )
                audit.write("comparison-" + label + "-accepted.json", accepted)
                reader = indexed_reader(kb, source, parsed, navigation)
                prepared = [
                    prepare_page(page, source, parsed, navigation, reader, max_chars=maximum)
                    for page in accepted.pages
                ]
                audit.write("comparison-" + label + "-prepared.json", prepared)
    finally:
        audit.phase = "step3"
