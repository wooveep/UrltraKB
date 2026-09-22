"""Validate persisted W/T planning schedules against their immutable evidence."""

from __future__ import annotations

from typing import Any

from openkb.agent.document_range_validation import merged_intervals
from openkb.agent.document_window_receipts import window_receipt_id
from openkb.agent.document_windowing import target_intervals
from openkb.sources import content_id


def no_readable_body(parsed: Any) -> bool:
    """Whether parser facts leave no original text that planning may classify."""

    return any(
        isinstance(row, dict)
        and row.get("status") == "needs_review"
        and isinstance(row.get("reason"), str)
        and (row["reason"] == "empty_content" or "readable_text_absent" in row["reason"].split(";"))
        for row in getattr(parsed, "quality", [])
    )


def valid_window_schedule(
    schedule: Any,
    parsed: Any,
    *,
    source: Any | None = None,
    base_schedule: list[dict[str, Any]] | None = None,
) -> bool:
    """Accept only durable movable-T descriptors that remain in this parsing."""

    if not isinstance(schedule, list):
        return False
    if not schedule:
        return no_readable_body(parsed) or not any(
            getattr(block, "chars", 0) for block in getattr(parsed, "blocks", [])
        )
    try:

        def frozen_spans(values: list[Any]) -> list[tuple[int, int, int]]:
            return target_intervals(
                {
                    "target_start": 0,
                    "target_end": len(parsed.blocks),
                    "target_ranges": values,
                },
                parsed,
            )

        def descriptor_spans(descriptor: dict[str, Any]) -> list[tuple[int, int, int]]:
            return target_intervals(
                {
                    "target_start": descriptor["start"],
                    "target_end": descriptor["end"],
                },
                parsed,
            )

        def normalized(spans: list[tuple[int, int, int]]) -> dict[int, list[tuple[int, int]]]:
            result: dict[int, list[tuple[int, int]]] = {}
            for index, start, end in spans:
                result.setdefault(index, []).append((start, end))
            return {index: merged_intervals(rows) for index, rows in result.items()}

        def covers(
            allowed: list[tuple[int, int, int]], required: list[tuple[int, int, int]]
        ) -> bool:
            grouped = normalized(allowed)
            return all(
                any(left <= start and end <= right for left, right in grouped.get(index, []))
                for index, start, end in required
            )

        base_w: list[dict[str, Any]] = []
        if base_schedule is not None:
            if not isinstance(base_schedule, list):
                return False
            for base in base_schedule:
                if not isinstance(base, dict):
                    return False
                descriptor = base.get("evidence")
                if descriptor is not None:
                    if not isinstance(descriptor, dict):
                        return False
                    spans = descriptor_spans(descriptor)
                elif isinstance(base.get("frozen_ranges"), list):
                    spans = frozen_spans(base["frozen_ranges"])
                else:
                    spans = target_intervals(base, parsed)
                descriptor_id = descriptor.get("id") if isinstance(descriptor, dict) else None
                base_w.append(
                    {
                        "spans": spans,
                        "identities": {
                            value
                            for value in (
                                window_receipt_id(base),
                                descriptor_id,
                                base.get("frozen_evidence_id"),
                            )
                            if isinstance(value, str) and value
                        },
                    }
                )

        covered: dict[int, list[tuple[int, int]]] = {}
        for window in schedule:
            if (
                not isinstance(window, dict)
                or type(window.get("target_start")) is not int
                or type(window.get("target_end")) is not int
                or not 0 <= window["target_start"] < window["target_end"] <= len(parsed.blocks)
                or not (target_spans := target_intervals(window, parsed))
            ):
                return False
            descriptor = window.get("evidence")
            frozen_ranges = window.get("frozen_ranges")
            frozen_identity = window.get("frozen_evidence_id")
            reloaded_from = window.get("reloaded_from")
            w_spans = target_spans
            if descriptor is not None:
                if not isinstance(descriptor, dict):
                    return False
                if source is not None:
                    from openkb.navigation_evidence import evidence_descriptor

                    expected = evidence_descriptor(
                        source, parsed, descriptor.get("start"), descriptor.get("end")
                    )
                    if descriptor != expected:
                        return False
                w_spans = descriptor_spans(descriptor)
                if not covers(w_spans, target_spans):
                    return False
                if frozen_identity is not None and frozen_identity != descriptor.get("id"):
                    return False
                # A normal descriptor is its own immutable W receipt.  Do not
                # let a persisted arbitrary ``window_id`` replace it: that ID
                # becomes part of both planning cache and accepted-receipt
                # identity on recovery.
                if frozen_ranges is None and window.get("window_id") not in {
                    None,
                    descriptor.get("id"),
                }:
                    return False
                if frozen_ranges is not None:
                    target_ranges = window.get("target_ranges")
                    if (
                        not isinstance(frozen_ranges, list)
                        or not frozen_ranges
                        or not isinstance(target_ranges, list)
                        or not target_ranges
                    ):
                        return False
                    frozen = frozen_spans(frozen_ranges)
                    if any(
                        not descriptor["start"] <= index < descriptor["end"]
                        for index, _, _ in frozen
                    ):
                        return False
                    expected_window = content_id(
                        {
                            "frozen_evidence": frozen_identity,
                            "target_ranges": target_ranges,
                        }
                    )
                    if window.get("window_id") != expected_window:
                        return False
                if (
                    reloaded_from is not None
                    and frozen_ranges is None
                    and window.get("window_id") != descriptor["id"]
                ):
                    return False
            elif frozen_ranges is not None:
                # Output-pressure children have no independent descriptor:
                # their W is the original exact range list and its persisted
                # ID must therefore be its canonical content hash.
                if (
                    not isinstance(frozen_ranges, list)
                    or not frozen_ranges
                    or frozen_identity != content_id(frozen_ranges)
                ):
                    return False
                w_spans = frozen_spans(frozen_ranges)
                if not covers(w_spans, target_spans):
                    return False
                expected_window = content_id(
                    {"frozen_evidence": frozen_identity, "target_ranges": window["target_ranges"]}
                )
                if window.get("window_id") != expected_window:
                    return False
            elif frozen_identity is not None:
                # Every supported frozen W carries its exact range list.  A
                # bare frozen identity cannot prove which source evidence it
                # names, and would otherwise permit an arbitrary window ID.
                return False
            elif window.get("target_ranges") is not None and reloaded_from is None:
                # A capacity-bounded partial block has no descriptor or
                # output-split frozen W.  Its only durable W identity is the
                # source/version/parse bound range list emitted by
                # ``_bounded_once``.  Without this check an attacker can split
                # a previously accepted base range into fresh target fragments
                # and give them arbitrary receipt identities.
                if source is not None:
                    expected_window = content_id(
                        {
                            "source_id": source.source_id,
                            "version_id": source.id,
                            "parse_id": parsed.id,
                            "ranges": window["target_ranges"],
                        }
                    )
                    if window.get("window_id") != expected_window:
                        return False
            elif window.get("window_id") is not None and reloaded_from is None:
                # An ordinary descriptor-free full-block fallback has no
                # independent W receipt.  It must not invent one that later
                # changes cache/accepted-receipt identity.
                return False
            if reloaded_from is not None:
                if not isinstance(reloaded_from, str) or not reloaded_from:
                    return False
                if descriptor is None and frozen_ranges is None:
                    expected_window = content_id(
                        {
                            "reloaded_from": reloaded_from,
                            "target_ranges": window.get("target_ranges"),
                        }
                    )
                    if window.get("window_id") != expected_window:
                        return False
                if not any(
                    reloaded_from in base["identities"] and covers(base["spans"], w_spans)
                    for base in base_w
                ):
                    return False
            elif base_w and not any(
                normalized(w_spans) == normalized(base["spans"]) for base in base_w
            ):
                return False
            for index, start, end in target_spans:
                covered.setdefault(index, []).append((start, end))
        for index, block in enumerate(parsed.blocks):
            chars = getattr(block, "chars", None)
            if type(chars) is not int or chars < 0:
                return False
            block_intervals = covered.get(index, [])
            if chars and (
                sum(end - start for start, end in block_intervals) != chars
                or merged_intervals(block_intervals) != [(0, chars)]
            ):
                return False
            if not chars and block_intervals:
                return False
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return True
