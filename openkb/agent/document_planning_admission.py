"""Validate navigation and admit bounded planning windows."""

from __future__ import annotations

from typing import Any

from openkb.agent import document_planning_projection, document_planning_support, document_windowing


def validate_navigation(navigation: dict[str, Any] | None, source: Any, parsed: Any) -> None:
    if not navigation:
        return
    if not isinstance(navigation, dict):
        raise ValueError("Invalid navigation record")
    if navigation.get("source_id") and navigation["source_id"] != source.source_id:
        raise ValueError("Navigation source_id mismatch")
    navigation_version = navigation.get("version_id", navigation.get("version"))
    navigation_parse = navigation.get("parse_id", navigation.get("parse"))
    if navigation_version and navigation_version != source.id:
        raise ValueError("Navigation version_id mismatch")
    if navigation_parse and navigation_parse != parsed.id:
        raise ValueError("Navigation parse_id mismatch")
    if "nodes" in navigation:
        nodes = navigation["nodes"]
        if not isinstance(nodes, list):
            raise ValueError("Invalid navigation nodes")
        seen: set[str] = set()
        canonical = bool(nodes)
        fields = {
            "id", "parent", "start", "end", "title", "title_origin",
            "summary", "summary_origin", "structure_origin",
        }
        for node in nodes:
            if not isinstance(node, dict) or set(node) - fields:
                raise ValueError("Invalid navigation node")
            start, end = node.get("start"), node.get("end")
            if end is None and type(start) is int:
                end = start + 1
            if (
                type(start) is not int or type(end) is not int
                or not (
                    0 <= start < end <= len(parsed.blocks)
                    or len(parsed.blocks) == 0 and start == end == 0
                )
                or not isinstance(node.get("title"), str)
                or not isinstance(node.get("summary", ""), str)
            ):
                raise ValueError("Invalid navigation node")
            identity = node.get("id")
            if identity is not None:
                if not isinstance(identity, str) or not identity or identity in seen:
                    raise ValueError("Invalid navigation node identity")
                seen.add(identity)
            parent = node.get("parent")
            if parent is not None and (not isinstance(parent, str) or parent not in seen):
                raise ValueError("Invalid navigation node parent")
            canonical &= set(node) == fields
        if canonical:
            from openkb.navigation_tree import validate_nodes

            validate_nodes(nodes, len(parsed.blocks))


def admit_planning_windows(
    source: Any,
    parsed: Any,
    navigation: dict[str, Any] | None,
    settings: dict[str, Any],
    limits: Any,
    *,
    entity_types: list[str],
    schema: str,
    parser_conditions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Any]:
    total_blocks = len(parsed.blocks) if hasattr(parsed, "blocks") else 0
    planning_limits = document_planning_support.planning_admission_limits(limits)
    if not total_blocks or document_planning_support.no_readable_body(parsed):
        return [], planning_limits
    windows = navigation.get("windows", []) if navigation else []
    if navigation and "windows" in navigation:
        from openkb.navigation_evidence import planning_windows

        windows = planning_windows(source, parsed, navigation)
    if not windows:
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
        source_conditions=document_planning_projection.prompt_condition_template(parser_conditions),
    )
    return document_windowing.bounded_windows(
        source, parsed, windows, planning_limits, prompt_tokens=prompt_tokens
    )
