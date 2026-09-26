"""Nonblocking plan quality observations, distinct from execution outcomes."""

from collections import Counter
from difflib import SequenceMatcher

from openkb.agent.document_planning_pages import DEFAULT_PURPOSE, normalized_name


def quality_summary(state, overview):
    pages = state["pages"]
    deferred = state.get("deferred_suggestions", [])
    annotations = state.get("suggestion_annotations", {})
    warnings = []
    for index, page in enumerate(pages):
        if page["purpose"] == DEFAULT_PURPOSE:
            warnings.append({"kind": "independent_purpose_missing", "pages": [page["key"]]})
        label = normalized_name(page["title"])
        for prior in pages[:index]:
            other = normalized_name(prior["title"])
            if (
                label == other
                or min(len(label), len(other)) >= 6
                and SequenceMatcher(None, label, other).ratio() > 0.9
            ):
                warnings.append(
                    {
                        "kind": "classification_overlap"
                        if label == other and page["kind"] != prior["kind"]
                        else "possible_semantic_overlap",
                        "pages": [prior["key"], page["key"]],
                    }
                )
    warnings.extend({"kind": row["reason"], "suggestion": row["key"]} for row in deferred)
    if sum(line.startswith("# ") for line in overview.splitlines()) > 1:
        warnings.append({"kind": "overview_multiple_main_headings"})
    if len(overview) > 2400:
        warnings.append({"kind": "overview_length_review", "characters": len(overview)})
    hints = [hint for row in pages + deferred for hint in row.get("location_hints", [])]
    bound = [
        hint["value"]
        for hint in hints
        if isinstance(hint["value"], dict) and hint["value"].get("format") == "bound-location-v1"
    ]
    return {
        "quality_warnings": warnings,
        "classifications": dict(
            Counter(basis for row in annotations.values() for basis in set(row["basis"]))
        ),
        "annotations": {
            "notes_preserved": sum(
                len(row.get("planning_notes", row.get("notes", []))) for row in pages + deferred
            ),
            "response_origins": sum(len(row["origins"]) for row in annotations.values()),
            "preview_truncations": 0,
        },
        "location_bindings": {
            "bound_hints": len(bound),
            "bound_ranges": sum(len(row["ranges"]) for row in bound),
            "unresolved_expressions": sum(len(row["unresolved"]) for row in bound),
            "missing_request_mapping": sum(
                isinstance(hint["value"], dict) and "unbound_request_location" in hint["value"]
                for hint in hints
            ),
            "actual_evidence_ready": sum(row["state"] == "ready" for row in pages),
        },
    }
