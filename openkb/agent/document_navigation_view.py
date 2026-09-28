"""Project immutable navigation nodes onto the evidence actually sent to planning."""

from __future__ import annotations

import json
import math
from typing import Any

from openkb.navigation_metadata import hint_metadata
from openkb.processing import ProcessingIncomplete


def navigation_view(
    navigation: dict[str, Any] | None,
    evidence: dict[str, Any],
    limits: Any,
    parsed: Any | None = None,
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Keep chapter identities and paths, shortening summaries before locations."""
    nodes = navigation.get("nodes", []) if isinstance(navigation, dict) else []
    if not isinstance(nodes, list):
        return []
    by_id = {row.get("id"): row for row in nodes if isinstance(row, dict)}
    visible: dict[int, list[tuple[int, int, str, int]]] = {}
    for block in evidence.get("blocks", []):
        if not isinstance(block, dict):
            continue
        order, identity, body = block.get("order"), block.get("id"), block.get("text")
        if type(order) is not int or not isinstance(identity, str) or not isinstance(body, str):
            continue
        ref = block.get("reference") or {}
        start = ref.get("start", 0) if isinstance(ref, dict) else 0
        end = ref.get("end", len(body)) if isinstance(ref, dict) else len(body)
        if type(start) is int and type(end) is int and end - start == len(body):
            chars = (
                parsed.blocks[order].chars
                if parsed is not None and 0 <= order < len(parsed.blocks)
                else (end if not ref else -1)
            )
            visible.setdefault(order, []).append((start, end, identity, chars))

    def path(node: dict[str, Any]) -> list[str]:
        titles: list[str] = []
        seen: set[str] = set()
        current: dict[str, Any] | None = node
        while isinstance(current, dict):
            title = current.get("title")
            if isinstance(title, str) and title:
                titles.append(title)
            parent = current.get("parent")
            if not isinstance(parent, str) or parent in seen:
                break
            seen.add(parent)
            current = by_id.get(parent)
        return list(reversed(titles))

    rows: list[dict[str, Any]] = []
    for ordinal, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        start = node.get("start")
        end = node.get("end", start + 1 if type(start) is int else None)
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or start >= end
            or parsed is not None
            and end > len(parsed.blocks)
        ):
            continue
        selections: list[dict[str, Any]] = []
        complete = True
        run_first: str | None = None
        run_last: str | None = None

        def flush() -> None:
            nonlocal run_first, run_last
            if run_first is not None and run_last is not None:
                selections.append({"from_block": run_first, "through_block": run_last})
            run_first = run_last = None

        for index in range(start, end):
            pieces = visible.get(index, [])
            if not pieces:
                complete = False
                flush()
                continue
            for left, right, identity, block_end in pieces:
                if left == 0 and right == block_end:
                    if run_first is None:
                        run_first = identity
                    run_last = identity
                else:
                    complete = False
                    flush()
                    selections.append({"block": identity, "start_char": left, "end_char": right})
        flush()
        if not selections:
            visibility = "outside_window"
        else:
            visibility = "complete" if complete else "partial"
        identity = node.get("id")
        rows.append(
            {
                "section_key": f"section:{identity if isinstance(identity, str) else ordinal}",
                "title": node.get("title", ""),
                "heading_path": path(node),
                "ranges": selections,
                "visibility": visibility,
                "summary": node.get("summary", ""),
                "summary_origin": node.get("summary_origin", "unavailable"),
                **hint_metadata(node),
            }
        )
    # This is a token allowance for navigation within the full request. The
    # assembled request still passes RequestLimits' authoritative admission.
    capacity = max(128, min(8_000, limits.input_capacity // 4))

    def size() -> int:
        serialized = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        if model is not None:
            try:
                import litellm

                return int(litellm.token_counter(model=model, text=serialized))
            except Exception:
                pass
        return math.ceil(len(serialized) / 3)

    active = {row["section_key"] for row in rows if row["visibility"] != "outside_window"}
    ancestors: set[str] = set()
    for row in rows:
        if row["section_key"] not in active:
            continue
        node = by_id.get(row["section_key"].removeprefix("section:"))
        while isinstance(node, dict) and isinstance(node.get("parent"), str):
            parent = node["parent"]
            ancestors.add(f"section:{parent}")
            node = by_id.get(parent)
    protected = active | ancestors
    total_nodes = len(rows)
    summary_omitted = 0
    summary_truncated = 0
    # Start with the broad PageIndex map, then shed unrelated detail before
    # touching summaries needed to understand the current target.
    for row in list(reversed(rows)):
        if size() <= capacity:
            break
        if row["section_key"] not in protected and len(row["heading_path"]) > 2:
            rows.remove(row)
    for row in reversed(rows):
        if size() <= capacity:
            break
        if row["section_key"] not in protected and row["summary"]:
            row["summary"] = ""
            summary_omitted += 1
    for row in list(reversed(rows)):
        if size() <= capacity:
            break
        if row["section_key"] not in protected and len(row["heading_path"]) > 1:
            rows.remove(row)
    for row in rows:
        if size() <= capacity:
            break
        if row["section_key"] in protected and len(row["summary"]) > 160:
            row["summary"] = row["summary"][:159] + "…"
            row["summary_truncated"] = True
            summary_truncated += 1
    if size() > capacity:
        raise ProcessingIncomplete("planning_navigation_exceeds_request_budget", "planning")
    if rows:
        rows[0]["navigation_projection"] = {
            "total_nodes": total_nodes,
            "shown_nodes": len(rows),
            "summary_omitted": summary_omitted,
            "summary_truncated": summary_truncated,
            "navigation_complete": len(rows) == total_nodes,
        }
        for row in list(reversed(rows)):
            if size() <= capacity:
                break
            if row["section_key"] not in protected:
                rows.remove(row)
                rows[0]["navigation_projection"]["shown_nodes"] = len(rows)
                rows[0]["navigation_projection"]["navigation_complete"] = False
        if size() > capacity:
            raise ProcessingIncomplete("planning_navigation_exceeds_request_budget", "planning")
    return rows
