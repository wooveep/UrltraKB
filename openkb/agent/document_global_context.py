"""Freeze one derived planning context and admit PageIndex topic tasks against it."""

from __future__ import annotations

import json
from copy import deepcopy

from openkb.agent.document_planning_overview import current_overview
from openkb.agent.document_protocol import plan_messages
from openkb.processing import InputTooLarge
from openkb.sources import content_id

STRATEGY = "global-after-overview-v1"


def navigation_rows(navigation, parsed):
    """Whole-source locations; no source text or invented evidence aliases."""
    nodes = (navigation or {}).get("nodes", [])
    by_id = {str(node.get("id", i)): node for i, node in enumerate(nodes)}
    rows = []
    for i, node in enumerate(nodes):
        titles, seen, current = [], set(), node
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            titles.append(current.get("title", ""))
            current = by_id.get(str(current.get("parent")))
        start, end = node.get("start"), node.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(parsed.blocks)
        ):
            continue
        rows.append(
            {
                "section_key": f"section:{node.get('id', i)}",
                "title": node.get("title", ""),
                "heading_path": list(reversed(titles)),
                "parent": f"section:{node['parent']}" if str(node.get("parent")) in by_id else None,
                "summary": node.get("summary", ""),
                "original_ranges": [[start, end]],
            }
        )
    return rows


def empty_evidence(source, parsed):
    return {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "blocks": [],
    }


def _carry(state, detail=True, shown=None):
    count = len(state["pages"]) if shown is None else shown
    annotations = state.get("suggestion_annotations", {})
    return {
        "pages": [
            {
                "title": row["title"],
                "kind": row.get("kind"),
                "type": row.get("type"),
                "notes": row.get("planning_notes", []),
                **(
                    {
                        "purpose": row.get("purpose", ""),
                        "notes": row.get("planning_notes", []),
                        "aliases": annotations.get(row["key"], {}).get("aliases", []),
                    }
                    if detail
                    else {}
                ),
            }
            for row in state["pages"][:count]
        ],
        # Deferred conditions must survive compression; their titles are not
        # confirmed pages that later groups may silently promote.
        "suggestions": {
            "total": len(state["pages"]),
            "shown": count,
            "omitted": count < len(state["pages"]),
            "details_clipped": not detail,
        },
        "deferred_suggestions": [
            {key: row.get(key) for key in ("title", "kind", "reason", "purpose", "notes")}
            for row in state.get("deferred_suggestions", [])
        ],
    }


def messages_for(
    snapshot,
    rows,
    state,
    source,
    parsed,
    settings,
    limits,
    entity_types,
    schema,
    conditions,
    *,
    recovery="",
    detail=True,
    shown=None,
):
    context = json.loads(snapshot["context_json"])
    task = {
        "kind": "global_pages" if len(rows) == len(snapshot["nodes"]) else "topic_group",
        "snapshot_id": snapshot["id"],
        "sections": [row["section_key"] for row in rows],
        "ranges": [[0, len(parsed.blocks)]]
        if len(rows) == len(snapshot["nodes"])
        else [value for row in rows for value in row["original_ranges"]],
        "total_blocks": len(parsed.blocks),
    }
    return plan_messages(
        empty_evidence(source, parsed),
        _carry(state, detail, shown),
        task,
        rows,
        "",
        entity_types,
        schema,
        settings.get("language", ""),
        [row["target"] for row in context["catalog"]],
        source_conditions=conditions,
        subtask="pages",
        recovery=recovery,
        planning_context=context,
    )


def fits(messages, settings, limits, reserve=0):
    try:
        _, tokens = limits.request(settings["model"], messages, {})
        return tokens + reserve <= limits.input_capacity
    except InputTooLarge:
        return False


def split_topics(rows):
    """Partition by actual tree branches, or consecutive flat nodes, without W."""
    if len(rows) <= 1:
        return []
    keys = {row["section_key"] for row in rows}
    roots = [row for row in rows if row["parent"] not in keys]
    if len(roots) > 1:
        root_keys = {row["section_key"] for row in roots}
        by_key = {row["section_key"]: row for row in rows}
        groups = {row["section_key"]: [] for row in roots}
        for row in rows:
            current, seen = row, set()
            while current["section_key"] not in root_keys and current["section_key"] not in seen:
                seen.add(current["section_key"])
                current = by_key[current["parent"]]
            groups[current["section_key"]].append(row)
        return list(groups.values())
    # Root introduction and its actual child branches retain their own identity.
    if len(roots) == 1:
        descendants = [row for row in rows if row is not roots[0]]
        return [[roots[0]], *(split_topics(descendants) or [descendants])]
    middle = max(1, len(rows) // 2)
    return [rows[:middle], rows[middle:]]


def freeze_context(
    state, navigation, source, parsed, catalog, settings, limits, entity_types, schema, conditions
):
    if state.get("planning_snapshot"):
        return state["planning_snapshot"]
    nodes = navigation_rows(navigation, parsed)
    roots = [row for row in nodes if row["parent"] is None]
    if len(roots) == 1:
        roots += [row for row in nodes if row["parent"] == roots[0]["section_key"]]
    context = {
        "protocol": "global-planning-context-v1",
        "strategy": STRATEGY,
        "source": {"source_id": source.source_id, "version_id": source.id, "parse_id": parsed.id},
        "navigation_id": content_id((navigation or {}).get("nodes", [])),
        "overview": {
            "text": current_overview(state),
            "input_clipped": False,
            "missing_windows": sum(
                task.get("status") != "accepted"
                for key, task in state["tasks"].items()
                if key.endswith(":overview") and task.get("status") != "retired"
            ),
        },
        "topics": [
            {key: row[key] for key in ("section_key", "title", "summary", "heading_path")}
            for row in roots
        ],
        "catalog": [
            {"target": path, "title": title, "brief": brief} for path, title, brief in catalog
        ],
        "projection": {
            "total_nodes": len(nodes),
            "overview_clipped": False,
            "detail_nodes_in_suffix": True,
            "omitted_nodes": len(nodes) - len(roots),
            "shortened_summaries": 0,
            "omitted_catalog": 0,
        },
    }

    # Store canonical serialization, so checkpoint JSON key sorting cannot
    # change P's bytes or its wire identity ordering on resume.
    def snapshot():
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "id": content_id({"context": encoded, "nodes": nodes, "catalog": catalog}),
            "context_json": encoded,
            "nodes": nodes,
            "catalog": catalog,
        }

    def minimal_fits():
        minimal = [{**row, "summary": ""} for row in nodes[:1]]
        return fits(
            messages_for(
                snapshot(),
                minimal,
                state,
                source,
                parsed,
                settings,
                limits,
                entity_types,
                schema,
                conditions,
            ),
            settings,
            limits,
            reserve=min(800, limits.input_capacity // 8),
        )

    for length in (240, 80, 0):
        if minimal_fits():
            break
        for row in context["topics"]:
            row["summary"] = row["summary"][:length]
        context["projection"]["shortened_summaries"] = sum(
            a["summary"] != b["summary"] for a, b in zip(roots, context["topics"])
        )
        for row in context["catalog"]:
            row.pop("brief", None)
    while not minimal_fits() and context["catalog"]:
        context["catalog"] = context["catalog"][: len(context["catalog"]) // 2]
        context["projection"]["omitted_catalog"] = len(catalog) - len(context["catalog"])
    while not minimal_fits() and len(context["overview"]["text"]) > 300:
        text = context["overview"]["text"]
        paragraphs = text.split("\n\n")
        context["overview"]["text"] = (
            "\n\n".join(paragraphs[: max(1, len(paragraphs) // 2)])
            if len(paragraphs) > 1
            else text[: len(text) // 2]
        )
        context["overview"]["input_clipped"] = True
        context["projection"]["overview_clipped"] = True
    result = snapshot()
    state["planning_snapshot"] = result
    state["planning_strategy"] = STRATEGY
    return result


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("context_json"), str):
        return False
    if not isinstance(snapshot.get("nodes"), list) or not isinstance(snapshot.get("catalog"), list):
        return False
    if snapshot.get("id") != content_id(
        {
            "context": snapshot["context_json"],
            "nodes": snapshot["nodes"],
            "catalog": snapshot["catalog"],
        }
    ):
        return False
    try:
        context = json.loads(snapshot["context_json"])
        return context["strategy"] == STRATEGY and isinstance(context["overview"]["text"], str)
    except (ValueError, KeyError, TypeError):
        return False


def task_record(snapshot, rows, *, family=None):
    key = "global-pages:" + content_id(
        {
            "strategy": STRATEGY,
            "snapshot": snapshot["id"],
            "sections": [row["section_key"] for row in rows],
        }
    )
    return key, {
        "component": "pages",
        "kind": "global_pages" if len(rows) == len(snapshot["nodes"]) else "topic_group",
        "sections": deepcopy(rows),
        "snapshot_id": snapshot["id"],
        "family": family or key,
        "status": "pending",
        "attempts": 0,
        "raw": None,
        "reason": None,
    }
