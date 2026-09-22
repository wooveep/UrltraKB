"""Capacity-aware subdivision of formal document-planning windows."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan import table_row_identity
from openkb.agent.evidence_units import JSON_FORMAT
from openkb.processing import InputTooLarge, ProcessingIncomplete, RequestLimits
from openkb.sources import content_id


@dataclass(frozen=True)
class PlanningView:
    """The bounded catalog and cumulative-state view carried by one W/S/T request."""

    page_register: list[dict[str, Any]]
    open_references: list[dict[str, Any]]
    catalog_entries: list[tuple[str, str]]
    messages: list[dict[str, Any]]


def _table_row_key(block: Any, start: int, end: int) -> tuple[Any, ...] | None:
    """Identify a complete native table row that must enter planning together."""

    if start != 0 or end != getattr(block, "chars", None):
        return None
    return table_row_identity(getattr(block, "location", {}))


def target_intervals(window: dict[str, Any], parsed: Any) -> list[tuple[int, int, int]]:
    """Expand one movable T into exact source intervals without changing W."""

    target_start, target_end = window.get("target_start"), window.get("target_end")
    if (
        type(target_start) is not int
        or type(target_end) is not int
        or not 0 <= target_start < target_end <= len(parsed.blocks)
    ):
        raise ValueError("Invalid bounded planning target window")
    values = window.get("target_ranges")
    if values is None:
        values = [[window["target_start"], window["target_end"]]]
    result = []
    for value in values:
        if isinstance(value, dict):
            index, start, end = (
                value.get("block_index"),
                value.get("start_char"),
                value.get("end_char"),
            )
            if not (
                type(index) is int
                and type(start) is int
                and type(end) is int
                and 0 <= index < len(parsed.blocks)
                and 0 <= start < end <= parsed.blocks[index].chars
            ):
                raise ValueError("Invalid bounded planning target range")
            if not target_start <= index < target_end:
                raise ValueError("Bounded planning target range leaves its block window")
            result.append((index, start, end))
            continue
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or any(type(item) is not int for item in value)
            or not 0 <= value[0] < value[1] <= len(parsed.blocks)
        ):
            raise ValueError("Invalid bounded planning block range")
        if value[0] < target_start or value[1] > target_end:
            raise ValueError("Bounded planning target range leaves its block window")
        result.extend((index, 0, parsed.blocks[index].chars) for index in range(*value))
    return [(index, start, end) for index, start, end in result if end > start]


def _split_target_intervals(
    parsed: Any, intervals: list[tuple[int, int, int]]
) -> list[list[tuple[int, int, int]]]:
    """Bisect exact target intervals without separating a complete table row."""

    groups: list[list[tuple[int, int, int]]] = []
    row_key = None
    for interval in intervals:
        key = _table_row_key(parsed.blocks[interval[0]], interval[1], interval[2])
        if groups and key is not None and key == row_key:
            groups[-1].append(interval)
        else:
            groups.append([interval])
            row_key = key
    if len(groups) > 1:
        split = min(
            range(1, len(groups)),
            key=lambda index: abs(2 * sum(len(group) for group in groups[:index]) - len(intervals)),
        )
        pieces = [
            [item for group in groups[:split] for item in group],
            [item for group in groups[split:] for item in group],
        ]
    elif (
        len(intervals) == 1
        and _table_row_key(parsed.blocks[intervals[0][0]], *intervals[0][1:]) is None
    ):
        index, start, end = intervals[0]
        midpoint = start + (end - start) // 2
        if midpoint <= start or midpoint >= end:
            return []
        pieces = [[(index, start, midpoint)], [(index, midpoint, end)]]
    else:
        return []
    return pieces


def split_planning_target(window: dict[str, Any], parsed: Any) -> list[dict[str, Any]]:
    """Split only T after output pressure; retain the original frozen W receipt."""

    intervals = target_intervals(window, parsed)
    pieces = _split_target_intervals(parsed, intervals)
    if not pieces:
        return []
    frozen_ranges = window.get("frozen_ranges") or [
        {"block_index": index, "start_char": start, "end_char": end}
        for index, start, end in intervals
    ]
    frozen = window.get("frozen_evidence_id") or (window.get("evidence") or {}).get("id")
    frozen = frozen or content_id(frozen_ranges)
    children = []
    for piece in pieces:
        ranges = [
            {"block_index": index, "start_char": start, "end_char": end}
            for index, start, end in piece
        ]
        children.append(
            {
                **window,
                "target_start": min(index for index, _, _ in piece),
                "target_end": max(index for index, _, _ in piece) + 1,
                "target_ranges": ranges,
                "frozen_ranges": frozen_ranges,
                "frozen_evidence_id": frozen,
                "window_id": content_id({"frozen_evidence": frozen, "target_ranges": ranges}),
            }
        )
    return children


def reload_planning_target(
    source: Any, parsed: Any, window: dict[str, Any]
) -> list[dict[str, Any]]:
    """Replace an unaccepted W/T with smaller, independently frozen children.

    This is distinct from an output split: the current request cannot carry a
    required historical page/reference alongside its W, so each child obtains
    a fresh exact W before any ledger delta has been accepted.  It is bounded
    by the same table-aware bisection as ordinary target splitting.
    """

    intervals = target_intervals(window, parsed)
    pieces = _split_target_intervals(parsed, intervals)
    if not pieces:
        return []
    from openkb.agent.document_window_receipts import window_receipt_id
    from openkb.navigation_evidence import evidence_descriptor

    # Keep reload descendants bound to the original W rather than to an
    # intermediate child.  Recovery can then authenticate every fresh smaller
    # W against the immutable schedule that existed before planning began.
    parent = (
        window.get("reloaded_from")
        or window.get("frozen_evidence_id")
        or (window.get("evidence") or {}).get("id")
        or window.get("window_id")
        or window_receipt_id(window)
    )
    base = {
        key: value
        for key, value in window.items()
        if key
        not in {"evidence", "target_ranges", "frozen_ranges", "frozen_evidence_id", "window_id"}
    }
    children = []
    for piece in pieces:
        start, end = min(item[0] for item in piece), max(item[0] for item in piece) + 1
        complete = [item[0] for item in piece] == list(range(start, end)) and all(
            left == 0 and right == parsed.blocks[index].chars for index, left, right in piece
        )
        ranges = [
            {"block_index": index, "start_char": left, "end_char": right}
            for index, left, right in piece
        ]
        if complete:
            descriptor = evidence_descriptor(source, parsed, start, end)
            children.append(
                {
                    **base,
                    "evidence": descriptor,
                    "target_start": start,
                    "target_end": end,
                    "window_id": descriptor["id"],
                    "reloaded_from": parent,
                }
            )
        else:
            children.append(
                {
                    **base,
                    "evidence": None,
                    "target_start": start,
                    "target_end": end,
                    "target_ranges": ranges,
                    "window_id": content_id({"reloaded_from": parent, "target_ranges": ranges}),
                    "reloaded_from": parent,
                }
            )
    return children


def bounded_windows(
    source: Any,
    parsed: Any,
    windows: list[dict[str, Any]],
    limits: RequestLimits,
    *,
    prompt_tokens: int = 0,
) -> tuple[list[dict[str, Any]], RequestLimits]:
    """Derive smaller frozen groups only when a saved group cannot fit a full request.

    The estimate intentionally reserves the configured output and the required
    accounting suffix before reading a large PageIndex group into memory.  It is
    a capacity admission estimate, not a new universal context ceiling: every
    model/configuration produces its own derived boundary and the actual
    request budget remains authoritative at dispatch.
    """

    # First use every configured capacity step before splitting an otherwise
    # complete source window.  A larger configured context is a safe planning
    # admission option; it is not a claim about an unconfigured provider limit.
    current = limits
    while True:
        # A shared-context declaration caps the *whole* request, while this
        # loop compares only the available input portion.  Keep the reserved
        # output fixed while growing a planning request, so a 10k shared
        # context with a 100-token completion can use up to 9.9k input tokens
        # before semantic subdivision is necessary.  Independent contracts
        # already express that ceiling directly as ``max_input_tokens``.
        max_context_tokens = (
            current.max_input_tokens or current.input_capacity
            if not current.shared_context
            else (current.max_context_tokens or current.context_tokens) - current.output_tokens
        )
        try:
            bounded = _bounded_once(source, parsed, windows, current, prompt_tokens=prompt_tokens)
        except ProcessingIncomplete as exc:
            if (
                exc.reason != "planning_context_exceeds_request_budget"
                or current.input_capacity >= max_context_tokens
            ):
                raise
            current = current.expanded(reason="input_budget_exceeded")
            continue
        if len(bounded) <= len(windows) or current.input_capacity >= max_context_tokens:
            return bounded, current
        current = current.expanded(reason="input_budget_exceeded")


def _bounded_once(
    source: Any,
    parsed: Any,
    windows: list[dict[str, Any]],
    limits: RequestLimits,
    *,
    prompt_tokens: int,
) -> list[dict[str, Any]]:
    # ``prompt_tokens`` is measured from the actual immutable system/rules,
    # catalogue, schema and task envelope.  Use the very same margin as the
    # later assembled-view gate: otherwise a W admitted in the small gap
    # between two estimates can fail before S/D are even projected.
    from openkb.agent.document_protocol import calculate_plan_budget

    effective_capacity = (
        limits.context_tokens
        if limits.shared_context
        else limits.input_capacity + limits.output_tokens
    )
    input_tokens = (
        calculate_plan_budget(effective_capacity, limits.output_tokens, fixed_overhead=0)[
            "available_for_payload"
        ]
        - prompt_tokens
    )
    if input_tokens <= 0:
        # The immutable planning envelope alone cannot fit.  This is an
        # expected bounded-processing outcome, not malformed source data or an
        # internal crash; no source range can be safely classified until the
        # configured context/output budget is enlarged.
        raise ProcessingIncomplete("planning_context_exceeds_request_budget", "planning")

    def metadata_chars(index: int) -> int:
        block = parsed.blocks[index]
        return len(
            json.dumps(
                {
                    "id": block.id,
                    "order": block.order,
                    "kind": block.kind,
                    "location": block.location,
                    "assets": list(block.assets),
                    "context": block.context,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    def cost(index: int, start: int, end: int) -> int:
        # A roughly two-char/token wire estimate budgets both source text and
        # the per-block JSON envelope.  It is intentionally local to the
        # active model/request capacity rather than a universal block limit.
        return max(1, math.ceil((end - start + metadata_chars(index)) / 2))

    max_cost = input_tokens
    bounded: list[dict[str, Any]] = []
    from openkb.navigation_evidence import evidence_descriptor

    for window in windows:
        source_intervals = target_intervals(window, parsed)
        estimated = sum(cost(index, start, end) for index, start, end in source_intervals)
        if estimated <= max_cost:
            bounded.append(window)
            continue

        batch: list[tuple[int, int, int]] = []
        used = 0

        def flush() -> None:
            nonlocal batch, used
            if not batch:
                return
            start_block, end_block = min(row[0] for row in batch), max(row[0] for row in batch) + 1
            complete = [row[0] for row in batch] == list(range(start_block, end_block)) and all(
                start == 0 and end == parsed.blocks[index].chars for index, start, end in batch
            )
            derived = {
                **window,
                "target_start": start_block,
                "target_end": end_block,
                "target_tokens": max(1, used),
                "derived_from": (window.get("evidence") or {}).get("id"),
            }
            if complete:
                descriptor = evidence_descriptor(source, parsed, start_block, end_block)
                # Keep normal block windows compact on the wire; only a truly
                # partial block needs one RangeRef per exact target fragment.
                derived.pop("target_ranges", None)
                derived.update(evidence=descriptor, window_id=descriptor["id"])
            else:
                ranges = [
                    {"block_index": index, "start_char": start, "end_char": end}
                    for index, start, end in batch
                ]
                window_id = content_id(
                    {
                        "source_id": source.source_id,
                        "version_id": source.id,
                        "parse_id": parsed.id,
                        "ranges": ranges,
                    }
                )
                derived.update(evidence=None, target_ranges=ranges, window_id=window_id)
            bounded.append(derived)
            batch, used = [], 0

        def atomic_groups():
            current_key, current = None, []
            for interval in source_intervals:
                key = _table_row_key(parsed.blocks[interval[0]], interval[1], interval[2])
                if current and key is not None and key == current_key:
                    current.append(interval)
                    continue
                if current:
                    yield current_key, current
                current_key, current = key, [interval]
            if current:
                yield current_key, current

        for row_key, group in atomic_groups():
            if row_key is not None:
                group_cost = sum(cost(*interval) for interval in group)
                if group_cost > max_cost:
                    raise ProcessingIncomplete(
                        "evidence_context_exceeds_request_budget", "planning"
                    )
                if used + group_cost > max_cost and batch:
                    flush()
                batch.extend(group)
                used += group_cost
                if used >= max_cost:
                    flush()
                continue
            for index, start, end in group:
                cursor = start
                while cursor < end:
                    remaining = max_cost - used
                    if remaining <= 0:
                        flush()
                        remaining = max_cost
                    if cost(index, cursor, end) > remaining and batch:
                        flush()
                        continue
                    if cost(index, cursor, end) <= remaining:
                        finish = end
                    else:
                        available_chars = 2 * remaining - metadata_chars(index)
                        if available_chars <= 0:
                            raise ProcessingIncomplete(
                                "evidence_context_exceeds_request_budget", "planning"
                            )
                        finish = min(end, cursor + available_chars)
                    batch.append((index, cursor, finish))
                    used += cost(index, cursor, finish)
                    cursor = finish
                    if used >= max_cost:
                        flush()
        flush()
    return bounded


def project_planning_view(
    *,
    model: str,
    limits: RequestLimits,
    page_register: list[dict[str, Any]],
    open_references: list[dict[str, Any]],
    catalog_entries: list[tuple[str, str]],
    required_page_keys: set[str] | None = None,
    required_unresolved_keys: set[str] | None = None,
    required_catalog_targets: set[str] | None = None,
    assemble: Callable[
        [list[dict[str, Any]], list[dict[str, Any]], list[tuple[str, str]]], list[dict[str, Any]]
    ],
) -> PlanningView:
    """Project S and the catalog by actual request budget without discarding their ledger.

    The complete register remains in the durable plan. This only chooses the
    relevant view that can travel with one frozen-evidence request; the model
    cannot extend an uncarried page key or select an unprovided catalog target.
    """

    from openkb.agent.document_protocol import calculate_plan_budget

    capacity = (
        limits.context_tokens
        if limits.shared_context
        else limits.input_capacity + limits.output_tokens
    )
    budget = calculate_plan_budget(capacity, limits.output_tokens, fixed_overhead=0)

    def fits(
        pages: list[dict[str, Any]],
        unresolved: list[dict[str, Any]],
        catalog: list[tuple[str, str]],
    ) -> bool:
        messages = assemble(pages, unresolved, catalog)
        try:
            import litellm

            tokens = litellm.token_counter(model=model, messages=messages)
        except Exception:
            tokens = math.ceil(
                sum(len(str(message.get("content", ""))) for message in messages) / 3
            )
        # The serialized request already contains W + F + S + D. Prefer the
        # calibrated safety margin, but do not turn it into a universal input
        # ceiling: the configured request gate is authoritative when reclaiming
        # that optional headroom is necessary.
        if budget["fits"](0, 0, tokens):
            return True
        try:
            limits.request(model, messages, {"response_format": JSON_FORMAT})
        except InputTooLarge:
            return False
        return True

    required_page_keys = required_page_keys or set()
    required_unresolved_keys = required_unresolved_keys or set()
    required_catalog_targets = required_catalog_targets or set()
    selected_pages = [page for page in page_register if page.get("key") in required_page_keys]
    selected_unresolved = [
        reference
        for reference in open_references
        if reference.get("key") in required_unresolved_keys
    ]
    selected_catalog = [entry for entry in catalog_entries if entry[0] in required_catalog_targets]
    if not fits(selected_pages, selected_unresolved, selected_catalog):
        # The caller must replace this unaccepted W/T with a smaller targeted
        # reload.  Dropping an explicit page/reference would let a later
        # request create a duplicate or leave a dependency unbound.
        if selected_pages or selected_unresolved or selected_catalog:
            raise ProcessingIncomplete("planning_relevant_state_exceeds_request_budget", "planning")
        raise ProcessingIncomplete("planning_context_exceeds_request_budget", "planning")
    for page in page_register:
        if page in selected_pages:
            continue
        if fits([*selected_pages, page], selected_unresolved, selected_catalog):
            selected_pages.append(page)
    for reference in open_references:
        if reference in selected_unresolved:
            continue
        if fits(selected_pages, [*selected_unresolved, reference], selected_catalog):
            selected_unresolved.append(reference)
    for entry in catalog_entries:
        if entry in selected_catalog:
            continue
        if fits(selected_pages, selected_unresolved, [*selected_catalog, entry]):
            selected_catalog.append(entry)
    return PlanningView(
        selected_pages,
        selected_unresolved,
        selected_catalog,
        assemble(selected_pages, selected_unresolved, selected_catalog),
    )


def planning_prompt_tokens(
    source: Any,
    parsed: Any,
    settings: dict[str, Any],
    *,
    entity_types: list[str],
    schema: str,
    source_conditions: list[dict[str, str]],
) -> int:
    """Measure fixed W/S/T protocol overhead before deriving source windows."""

    try:
        import litellm

        from openkb.agent.document_protocol import plan_messages

        messages = plan_messages(
            evidence={
                "group_id": "planning-capacity",
                "source_id": source.source_id,
                "version_id": source.id,
                "parse_id": parsed.id,
                "blocks": [],
            },
            carry_s={"overview": "", "page_register": [], "open_references": []},
            target_t={"target_start": 0, "target_end": max(1, len(parsed.blocks))},
            navigation_hints=[],
            catalog_window="",
            entity_types=entity_types,
            schema=schema,
            language=settings.get("language", ""),
            catalog_targets=[],
            source_conditions=source_conditions,
        )
        return litellm.token_counter(model=settings["model"], messages=messages)
    except Exception:
        return 0
