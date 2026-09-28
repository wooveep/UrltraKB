"""Freeze one derived planning context and admit PageIndex topic tasks against it."""

from __future__ import annotations

import json
from copy import deepcopy

from openkb.agent.document_planning_overview import current_overview
from openkb.agent.document_protocol import plan_messages
from openkb.navigation_metadata import hint_metadata, structure_diagnostics
from openkb.processing import InputTooLarge
from openkb.sources import content_id

STRATEGY = "global-navigation-v3"


def navigation_rows(navigation, parsed):
    """Whole-source locations; no source text or invented evidence aliases."""
    from openkb.agent.document_planning_support import exclude_attachment_ranges

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
        ranges, _ = exclude_attachment_ranges(parsed, [[start, end]])
        if not ranges:
            continue
        rows.append(
            {
                "section_key": f"section:{node.get('id', i)}",
                "title": node.get("title", ""),
                "heading_path": list(reversed(titles)),
                "parent": f"section:{node['parent']}" if str(node.get("parent")) in by_id else None,
                "summary": node.get("summary") or "",
                "summary_origin": node.get("summary_origin", "unspecified"),
                "original_ranges": ranges,
                **hint_metadata(node),
            }
        )
    keys = {row["section_key"] for row in rows}
    for row in rows:
        if row["parent"] not in keys:
            row["parent"] = None
    return rows


def empty_evidence(source, parsed):
    return {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "blocks": [],
    }


def _carry(state, detail=True, shown=None):
    pages, deferred = state["pages"], state.get("deferred_suggestions", [])
    total = len(pages) + len(deferred)
    count = total if shown is None else min(shown, total)
    annotations = state.get("suggestion_annotations", {})

    def details(row, notes):
        purpose = row.get("purpose", "")
        return {
            "purpose": purpose if detail else purpose[:200],
            "notes": notes if detail else [note[:160] for note in notes[:2]],
        }

    return {
        "pages": [
            {
                "title": row["title"],
                "kind": row.get("kind"),
                "type": row.get("type"),
                **(
                    details(row, row.get("planning_notes", []))
                    if detail
                    else {"notes": [note[:160] for note in row.get("planning_notes", [])[:2]]}
                ),
                **(
                    {
                        "aliases": annotations.get(row["key"], {}).get("aliases", []),
                    }
                    if detail
                    else {}
                ),
            }
            for row in pages[:count]
        ],
        # Deferred conditions must survive compression; their titles are not
        # confirmed pages that later groups may silently promote.
        "suggestions": {
            "total": total,
            "accepted_total": len(pages),
            "deferred_total": len(deferred),
            "shown": count,
            "omitted": count < total,
            "details_clipped": not detail,
        },
        "deferred_suggestions": [
            {
                **{key: row.get(key) for key in ("title", "kind", "reason")},
                **details(row, row.get("notes", [])),
            }
            for row in deferred[: max(0, count - len(pages))]
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
    overview_limit=None,
):
    from openkb.agent.document_planning_support import exclude_attachment_ranges

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
    task["ranges"], _ = exclude_attachment_ranges(parsed, task["ranges"])
    carry = _carry(state, detail, shown)
    if state.get("runtime"):
        from openkb.agent.document_planning_runtime import selection_counts

        carry["selection_counts"] = selection_counts(state["pages"], state["catalog_targets"])
        if state["runtime"]["kb_stage"] == "initial":
            carry["suggested_new_concepts_remaining"] = max(
                0, 3 - carry["selection_counts"]["concept"]["create"]
            )
    overview = current_overview(state)
    carry["overview"] = {
        "text": overview if overview_limit is None else overview[:overview_limit],
        "input_clipped": overview_limit is not None and len(overview) > overview_limit,
        "partial": not state.get("overview_snapshot")
        or bool(state["overview_snapshot"].get("partial")),
    }
    return plan_messages(
        empty_evidence(source, parsed),
        carry,
        task,
        rows if context["projection"]["detail_nodes_in_suffix"] else [],
        "",
        entity_types,
        schema,
        settings.get("language", ""),
        context.get("common_inputs", {}).get(
            "existing_targets", [row["target"] for row in context["catalog"]]
        ),
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
    def projected_runtime(context):
        runtime = deepcopy(state.get("runtime", {}))
        if runtime:
            runtime["catalog_status"].update(
                shown=len(context["catalog"]),
                omitted=len(catalog) - len(context["catalog"]),
            )
        return runtime

    common_inputs = {
        "schema": schema,
        "entity_types": entity_types,
        "language": settings.get("language", ""),
        "source_conditions": conditions or [],
        "existing_pages": "",
        "existing_targets": [path for path, _, _ in catalog],
    }
    if state.get("planning_snapshot"):
        saved = state["planning_snapshot"]
        context = json.loads(saved["context_json"])
        if (
            context["source"]
            != {"source_id": source.source_id, "version_id": source.id, "parse_id": parsed.id}
            or saved["nodes"] != navigation_rows(navigation, parsed)
            or context["navigation_id"] != content_id((navigation or {}).get("nodes", []))
            or context.get("strategy") != STRATEGY
            or context.get("common_inputs") != common_inputs
            or content_id(saved["catalog"]) != content_id(catalog)
            or context.get("catalog_types", {}) != state.get("catalog_types", {})
            or saved.get("catalog_metadata", {}) != state.get("catalog_metadata", {})
            or context.get("runtime", {}) != projected_runtime(context)
            or context.get("structure_diagnostics") != structure_diagnostics(navigation or {})
        ):
            from openkb.processing import ProcessingIncomplete

            raise ProcessingIncomplete("planning_recovery_identity_mismatch", "planning")
        return saved
    nodes = navigation_rows(navigation, parsed)
    roots = [row for row in nodes if row["parent"] is None]
    if len(roots) == 1:
        roots += [row for row in nodes if row["parent"] == roots[0]["section_key"]]
    context = {
        "protocol": "global-planning-context-v3",
        "strategy": STRATEGY,
        "source": {"source_id": source.source_id, "version_id": source.id, "parse_id": parsed.id},
        "navigation_id": content_id((navigation or {}).get("nodes", [])),
        **(
            {"navigation_style": "legacy_pdf"}
            if any("pdf_page_range" in row for row in nodes)
            else {}
        ),
        "common_inputs": deepcopy(common_inputs),
        "catalog_types": deepcopy(state.get("catalog_types", {})),
        "structure_diagnostics": structure_diagnostics(navigation or {}),
        "summary_input": {
            "nodes": len(nodes),
            "with_summary": sum(bool(row["summary"]) for row in nodes),
            "missing_summary": sum(not row["summary"] for row in nodes),
            "provenance": "derived_navigation_not_original_text",
        },
        "topics": [dict(row) for row in nodes],
        "catalog": [
            {
                "target": path,
                "title": title,
                "brief": brief,
                **state.get("catalog_metadata", {}).get(path, {}),
                **(
                    {"type": state["catalog_types"][path]}
                    if path in state.get("catalog_types", {})
                    else {}
                ),
            }
            for path, title, brief in catalog
        ],
        "projection": {
            "total_nodes": len(nodes),
            "detail_nodes_in_suffix": False,
            "omitted_nodes": 0,
            "shortened_summaries": 0,
            "omitted_catalog": 0,
        },
    }

    # Store canonical serialization, so checkpoint JSON key sorting cannot
    # change P's bytes or its wire identity ordering on resume.
    def snapshot():
        if state.get("runtime"):
            context["runtime"] = projected_runtime(context)
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "id": content_id(
                {
                    "context": encoded,
                    "nodes": nodes,
                    "catalog": catalog,
                    "catalog_metadata": state.get("catalog_metadata", {}),
                }
            ),
            "context_json": encoded,
            "nodes": nodes,
            "catalog": catalog,
            "catalog_metadata": deepcopy(state.get("catalog_metadata", {})),
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

    if not minimal_fits():
        context["topics"] = [dict(row) for row in roots]
        context["projection"].update(
            detail_nodes_in_suffix=True, omitted_nodes=len(nodes) - len(roots)
        )
    if not minimal_fits():
        for row in context["catalog"]:
            row.pop("brief", None)
    context["projection"]["overview_requires_groups"] = not minimal_fits()
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
    if not minimal_fits() and len(roots) > 1:
        if len([row for row in nodes if row["parent"] is None]) == 1:
            # Roll children into their real parent, preserving global structure.
            context["topics"] = [{**context["topics"][0], "summary": ""}]
            context["projection"]["coarsened_nodes"] = len(roots) - 1
            context["projection"]["omitted_nodes"] = len(nodes) - 1
        else:
            # Flat/multiple-root directories have no shared semantic parent.
            # Consecutive spans cover every branch without inventing one.
            width = 2
            while True:
                groups = [roots[i : i + width] for i in range(0, len(roots), width)]
                context["topics"] = [
                    {
                        "from_section_key": group[0]["section_key"],
                        "through_section_key": group[-1]["section_key"],
                        "first_title": group[0]["title"][:120],
                        "last_title": group[-1]["title"][:120],
                        "branch_count": len(group),
                    }
                    for group in groups
                ]
                context["projection"].update(
                    coarsened_nodes=len(roots),
                    directory_segments=len(groups),
                    segment_titles_clipped=any(len(row["title"]) > 120 for row in roots),
                    omitted_nodes=len(nodes),
                )
                if minimal_fits() or len(groups) == 1:
                    break
                width *= 2
    result = snapshot()
    state["planning_snapshot"] = result
    state["planning_strategy"] = STRATEGY
    return result


def validate_snapshot(snapshot):
    def valid_range(value):
        if isinstance(value, list):
            return (
                len(value) == 2 and all(type(n) is int for n in value) and 0 <= value[0] < value[1]
            )
        return (
            isinstance(value, dict)
            and set(value) == {"block_index", "start_char", "end_char"}
            and all(type(n) is int for n in value.values())
            and value["block_index"] >= 0
            and 0 <= value["start_char"] < value["end_char"]
        )

    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("context_json"), str):
        return False
    if not isinstance(snapshot.get("nodes"), list) or not isinstance(snapshot.get("catalog"), list):
        return False
    if snapshot.get("id") != content_id(
        {
            "context": snapshot["context_json"],
            "nodes": snapshot["nodes"],
            "catalog": snapshot["catalog"],
            **(
                {"catalog_metadata": snapshot["catalog_metadata"]}
                if "catalog_metadata" in snapshot
                else {}
            ),
        }
    ):
        return False
    try:
        context = json.loads(snapshot["context_json"])
        nodes = snapshot["nodes"]
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("section_key"), str)
            or not all(isinstance(row.get(key), str) for key in ("title", "summary"))
            or not isinstance(row.get("heading_path"), list)
            or not all(isinstance(title, str) for title in row["heading_path"])
            or not (row.get("parent") is None or isinstance(row["parent"], str))
            or not isinstance(row.get("original_ranges"), list)
            or not row["original_ranges"]
            or not all(valid_range(span) for span in row["original_ranges"])
            for row in nodes
        ):
            return False
        return (
            context["strategy"] in {STRATEGY, "global-navigation-v2", "global-after-overview-v1"}
            and isinstance(context["topics"], list)
            and isinstance(context["catalog"], list)
            and isinstance(context["projection"], dict)
            and len({row["section_key"] for row in nodes}) == len(nodes)
            and all(
                isinstance(row, (list, tuple))
                and len(row) == 3
                and all(isinstance(item, str) for item in row)
                for row in snapshot["catalog"]
            )
        )
    except (ValueError, KeyError, TypeError):
        return False


def validate_global_state(state):
    """Validate the frozen task partition before a checkpoint can drive dispatch."""
    if state.get("planning_strategy") not in {
        STRATEGY,
        "global-navigation-v2",
        "global-after-overview-v1",
    }:
        return False
    tasks = state.get("tasks")
    if not isinstance(tasks, dict) or any(not isinstance(task, dict) for task in tasks.values()):
        return False
    snapshot = state.get("planning_snapshot")
    if snapshot is None:
        return "planning_tasks" not in state and not any(
            task.get("component") == "pages" and task.get("status") != "retired"
            for task in tasks.values()
        )
    keys = state.get("planning_tasks")
    if keys is None:
        return validate_snapshot(snapshot) and not any(
            task.get("component") == "pages" and task.get("status") != "retired"
            for task in tasks.values()
        )
    if (
        not validate_snapshot(snapshot)
        or not isinstance(keys, list)
        or not keys
        or not all(isinstance(key, str) and key in tasks for key in keys)
        or len(set(keys)) != len(keys)
    ):
        return False
    nodes = {row["section_key"]: row for row in snapshot["nodes"]}
    covered = []
    for key in keys:
        task = tasks[key]
        rows, family = task.get("sections"), task.get("family")
        if (
            task.get("component") != "pages"
            or task.get("status") == "retired"
            or task.get("snapshot_id") != snapshot["id"]
            or not isinstance(rows, list)
            or (nodes and not rows)
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("section_key"), str)
                or nodes.get(row["section_key"]) != row
                for row in rows
            )
            or not isinstance(family, str)
            or family not in tasks
        ):
            return False
        expected_key, expected = task_record(snapshot, rows)
        ancestor = tasks[family]
        if (
            expected_key != key
            or expected["kind"] != task.get("kind")
            or ancestor.get("family") != family
            or ancestor.get("snapshot_id") != snapshot["id"]
            or ancestor.get("component") != "pages"
            or (family != key and ancestor.get("status") != "retired")
        ):
            return False
        covered.extend(row["section_key"] for row in rows)
    return (
        len(covered) == len(nodes)
        and set(covered) == set(nodes)
        and set(keys)
        == {
            key
            for key, task in tasks.items()
            if task.get("component") == "pages" and task.get("status") != "retired"
        }
    )


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
