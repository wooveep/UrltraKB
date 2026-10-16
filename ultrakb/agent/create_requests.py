"""Partition frozen selections and prove exact input coverage before page creation."""

import hashlib
import json
from collections import defaultdict

from ultrakb.agent.page_response import PageResponseError
from ultrakb.compile_checkpoints import digest


def selection_key(window):
    location = {k: v for k, v in window.get("native_location", {}).items() if k != "offset"}
    return window["path"], window["sha256"], digest(location)


def coverage_id(window):
    return digest(
        {
            "source": selection_key(window),
            "offset": window["selection_offset"],
            "length": len(window["content"]),
            "content": hashlib.sha256(window["content"].encode()).hexdigest(),
        }
    )


def validate_coverage(expected, actual):
    """Compare complete reader selections, exact offsets and characters, not counts."""

    def selections(windows):
        groups = defaultdict(list)
        for window in windows:
            groups[selection_key(window)].append(window)
        result = {}
        for key, parts in groups.items():
            cursor, text = 0, []
            length = parts[0]["selection_length"]
            for part in parts:
                if part["selection_offset"] != cursor or part["selection_length"] != length:
                    raise PageResponseError(
                        "page_create_coverage_gap", "Noncontiguous original coverage"
                    )
                cursor += len(part["content"])
                text.append(part["content"])
            if cursor != length:
                raise PageResponseError("page_create_coverage_gap", "Selection tail is not covered")
            result[key] = "".join(text)
        return result

    if selections(expected) != selections(actual):
        raise PageResponseError("page_create_coverage_gap", "Partition changed original coverage")
    from ultrakb.content_blocks import markdown_layout

    # The reader can expose honest partial values, but a new executable excerpt
    # cannot silently become two unrelated snippets at a capacity boundary.
    text_by_source = selections(expected)
    groups = defaultdict(list)
    for window in actual:
        groups[selection_key(window)].append(window)
    for key, parts in groups.items():
        tokens, offsets = markdown_layout(text_by_source[key])
        fences = [
            (offsets[t.map[0]], offsets[t.map[1]]) for t in tokens if t.type == "fence" and t.map
        ]
        for part in parts[1:]:
            boundary = part["selection_offset"]
            if any(start < boundary < end for start, end in fences):
                raise PageResponseError(
                    "page_create_indivisible_excerpt",
                    "A necessary fenced excerpt exceeds one complete request; original retained",
                )


def original_message(originals, batch):
    """Only duplicate machine metadata is removed; factual text and tables survive."""
    projected = []
    for window in batch:
        value = {
            k: v
            for k, v in window.items()
            if k
            not in {
                "images",
                "reading_action",
                "figure_interpretation",
            }
        }
        if "units" in value:
            value["units"] = [
                {
                    k: v
                    for k, v in unit.items()
                    if k
                    not in {
                        "source_structure_policy",
                        "unit_kind",
                    }
                }
                for unit in value["units"]
            ]
        projected.append(value)
    message = originals.for_windows(batch)
    before = json.dumps(batch, ensure_ascii=False)
    after = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    from ultrakb.agent.evidence_payload import FIGURE_INTERPRETATION

    return {
        **message,
        "content": [
            {**b, "text": b["text"].replace(before, after) + "\n" + FIGURE_INTERPRETATION}
            for b in message["content"]
        ],
    }


def partition_originals(originals, windows, fits):
    """Re-read necessary selections at smaller bounds, never truncate a request."""
    initial = list(originals.windows)
    validate_coverage(initial, initial)
    if fits(initial, False):
        return [initial]
    if not initial:
        raise PageResponseError(
            "page_create_capacity_exceeded", "Indivisible current original is too large"
        )
    budget = 24000
    batch_windows = initial
    while any(not fits([part], True) for part in batch_windows):
        budget //= 2
        if budget < 32:
            raise PageResponseError(
                "page_create_capacity_exceeded", "Indivisible original metadata cannot fit"
            )
        unique = {selection_key(w): w for w in initial}
        batch_windows = []
        rejected = False
        for original in unique.values():
            location = {k: v for k, v in original["native_location"].items() if k != "offset"}
            for part in windows.iter_selection(original["path"], location, budget=budget):
                part = {**part, "source_role": original.get("source_role", "current")}
                batch_windows.append(part)
                if not fits([part], True):
                    rejected = True
                    break
            if rejected:
                break
        if rejected:
            continue
    validate_coverage(initial, batch_windows)
    batches, packed = [], []
    for part in batch_windows:
        if packed and not fits([*packed, part], True):
            batches.append(packed)
            packed = []
        packed.append(part)
    if packed:
        batches.append(packed)
    validate_coverage(initial, [w for b in batches for w in b])
    return batches
