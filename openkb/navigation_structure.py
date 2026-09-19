"""Ordered body windows yield starts; code owns hierarchy, extents and coverage."""

import re

import litellm

from openkb.agent.source_protocol import source_messages
from openkb.navigation_enhancement import IndexAllowanceExceeded, record_optional_failure
from openkb.navigation_evidence import evidence_descriptor, read_evidence_group
from openkb.navigation_requests import expand_capacity, request_value, summarize_group
from openkb.navigation_tree import validate_nodes
from openkb.processing import ProcessingIncomplete, processing_checkpoint
from openkb.sources import content_id

SYSTEM = """Analyze only the target body blocks in this window; overlap is context, not a
new target. Continue the supplied accepted section state. Native headings are reliable
anchors and must retain their original titles. Introduce inferred labels only for real
organization changes. Do not turn running headers, footers, a table of contents or ordinary
mentions into section starts. Return JSON {"sections":[{"title":"heading or label",
"title_origin":"source or inferred","level":1,"start_block":"block id","anchor":"short
verbatim excerpt within that block"}]}. Levels are 1 through 9. Return only NEW starts in
source order, without end ranges, offsets, explanations or historical tree edits. An empty
sections array is valid for continuation or when no finer structure is justified."""


def normalized(text):
    return " ".join(text.split())


def native_title(block):
    text = re.sub(r"^#{1,6}\s+", "", block["text"].strip())
    return normalized(re.sub(r"\n[=\-]+\s*$", "", text))


def validate_sections(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"sections"}
        or not isinstance(value["sections"], list)
    ):
        raise ValueError("Invalid structure response")
    for row in value["sections"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"title", "title_origin", "level", "start_block", "anchor"}
            or not isinstance(row["title"], str)
            or not 0 < len(row["title"]) <= 320
            or row["title_origin"] not in {"source", "inferred"}
            or type(row["level"]) is not int
            or not 1 <= row["level"] <= 9
            or not isinstance(row["start_block"], str)
            or not isinstance(row["anchor"], str)
            or not 0 < len(row["anchor"]) <= 320
        ):
            raise ValueError("Invalid structure start")


def located(row, blocks):
    block = blocks.get(row["start_block"])
    if block is None or block["kind"] in {"metadata", "image"}:
        return False
    if block["location"].get("role") in {"toc", "header", "footer"}:
        return False
    if normalized(row["anchor"]) not in normalized(block["text"]):
        return False
    if row["title_origin"] == "inferred":
        return True
    if block["kind"] == "heading":
        return normalized(row["title"]) == native_title(block)
    return any(
        normalized(row["title"]) == normalized(line)
        for line in block["text"].splitlines()
        if line.strip()
    )


def locate_problems(rows, evidence, settings, bundle, allowance, checkpoints, profile):
    locations, pending = {}, []
    candidates = [
        {"id": content_id({"scope": evidence["group_id"], "ordinal": i, "candidate": row}), **row}
        for i, row in enumerate(rows)
    ]
    for row in candidates:
        payload = {"stage": "index_location_once", "candidate": row}
        with checkpoints.request(SYSTEM, payload, dependencies=profile) as key:
            saved = checkpoints.load(key)
        if saved is None:
            pending.append(row)
        else:
            locations[row["id"]] = saved["location"]
    if pending:
        located_once = _locate_batch(
            pending, evidence, settings, bundle, allowance, checkpoints, profile
        )
        for row in pending:
            identity = row["id"]
            locations[identity] = located_once[identity]
            payload = {"stage": "index_location_once", "candidate": row}
            with checkpoints.request(SYSTEM, payload, dependencies=profile) as key:
                checkpoints.save(key, {"location": locations[identity]})
    return [
        {**original, **locations[candidate["id"]]} if locations[candidate["id"]] else None
        for original, candidate in zip(rows, candidates)
    ]


def _locate_batch(rows, evidence, settings, bundle, allowance, checkpoints, profile):
    if not rows:
        return []
    candidates = rows
    wanted = {row["id"] for row in candidates}

    def validate(value):
        if (
            not isinstance(value, dict)
            or set(value) != {"locations"}
            or not isinstance(value["locations"], list)
        ):
            raise ValueError("Invalid local locations")
        seen = set()
        for row in value["locations"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "location"}
                or not isinstance(row["id"], str)
                or row["id"] not in wanted
                or row["id"] in seen
            ):
                raise ValueError("Invalid local candidate")
            loc = row["location"]
            if loc is not None and (
                not isinstance(loc, dict)
                or set(loc) != {"start_block", "anchor"}
                or not all(isinstance(v, str) and v for v in loc.values())
                or len(loc["anchor"]) > 320
            ):
                raise ValueError("Invalid local position")
            seen.add(row["id"])
        if seen != wanted:
            raise ValueError("Missing local candidate")

    value = request_value(
        evidence,
        {"stage": "index_location", "candidates": candidates},
        "Locate only these problematic starts within the supplied original body. "
        "Do not rewrite the contents or hierarchy. Return "
        '{"locations":[{"id":"candidate id","location":{"start_block":'
        '"block id","anchor":"short original anchor"}}]}. Return location:null '
        "when unresolved. Ordinary mentions and TOC entries are not body starts.",
        settings,
        bundle,
        allowance,
        checkpoints,
        profile,
        validate,
    )
    return {row["id"]: row["location"] for row in value["locations"]}


def section_tree(source, parsed, sections):
    root = {
        "id": "n0",
        "parent": None,
        "start": 0,
        "end": len(parsed.blocks),
        "title": source.name,
        "title_origin": "source",
        "summary": "",
        "summary_origin": "unavailable",
        "structure_origin": "basic",
    }
    nodes, stack = [root], []
    for i, row in enumerate(sections):
        start = row["order"]
        while stack and stack[-1][0] >= row["level"]:
            stack.pop()
        next_order = next(
            (s["order"] for s in sections[i + 1 :] if s["level"] <= row["level"]),
            len(parsed.blocks),
        )
        node = {
            "id": "n" + content_id({"section": row, "ordinal": i})[:24],
            "parent": stack[-1][1] if stack else "n0",
            "start": start,
            "end": max(start + 1, next_order),
            "title": row["title"],
            "title_origin": row["title_origin"],
            "summary": "",
            "summary_origin": "unavailable",
            "structure_origin": "native"
            if parsed.blocks[start].kind == "heading" and row["title_origin"] == "source"
            else "inferred",
        }
        nodes.append(node)
        stack.append((row["level"], node["id"]))
    by_id = {node["id"]: node for node in nodes}
    for node in reversed(nodes[1:]):
        parent = by_id[node["parent"]]
        parent["end"] = max(parent["end"], node["end"])
    validate_nodes(nodes, len(parsed.blocks))
    return nodes


def _task(start, end, sections):
    ancestors = []
    for row in sections:
        while ancestors and ancestors[-1]["level"] >= row["level"]:
            ancestors.pop()
        ancestors.append(row)
    return {
        "stage": "index_structure",
        "target": {"start": start, "end": end},
        "continuation": {"version": content_id(sections), "sections": ancestors},
    }


def _window(kb, source, parsed, start, sections, settings, allowance, *, stop=None):
    target = (settings.get("navigation") or {}).get("window_tokens", 200000)
    accepted = [row for row in sections if row["order"] < start]

    def candidate(overlap, end):
        descriptor = evidence_descriptor(source, parsed, overlap, end)
        evidence = read_evidence_group(kb, source, parsed, descriptor)
        request = source_messages(evidence, _task(start, end, accepted), SYSTEM)
        prefix = request[-1]["content"].split(',"stage":', 1)[0]
        try:
            tokens = litellm.token_counter(model=settings["model"], text=prefix)
        except Exception as exc:
            raise ProcessingIncomplete("input_budget_unknown", "index_structure") from exc
        if type(tokens) is not int or tokens < 0:
            raise ProcessingIncomplete("input_budget_unknown", "index_structure")
        if tokens > target:
            return None
        fits = allowance.fits(settings["model"], request)
        while not fits and expand_capacity(allowance, "input_budget_exceeded"):
            fits = allowance.fits(settings["model"], request)
        return (descriptor, evidence, end, tokens) if fits else None

    overlap = max(0, start - 1)
    chosen = candidate(overlap, start + 1)
    if chosen is None and overlap < start:
        overlap = start
        chosen = candidate(overlap, start + 1)
    if chosen is None:
        raise IndexAllowanceExceeded("index_indivisible_range_exceeds_window")
    # Exponential growth and bisection avoid reserializing a 200k window once
    # per small parser fragment. At most two bounded candidates stay resident.
    low, high = start + 1, len(parsed.blocks) if stop is None else stop
    step = 2
    while low < high:
        end = min(start + step, high)
        value = candidate(overlap, end)
        if value is None:
            high = end - 1
            break
        chosen, low = value, end
        step *= 2
    while low < high:
        end = (low + high + 1) // 2
        value = candidate(overlap, end)
        if value is None:
            high = end - 1
        else:
            chosen, low = value, end
    return chosen


def infer_missing(kb_dir, source, parsed, record, settings, bundle, allowance, checkpoints):
    from openkb.navigation_toc import directory_sections

    sections, excluded, uncovered, unresolved = [], set(), [(0, len(parsed.blocks))], []
    try:
        sections, excluded, uncovered, unresolved = directory_sections(
            kb_dir, source, parsed, settings, bundle, allowance, checkpoints, record["profile"]
        )
    except (IndexAllowanceExceeded, ProcessingIncomplete) as exc:
        record_optional_failure(record, exc)
    if unresolved:
        record.update(status="degraded", reason="index_unlocated_contents")
    # Preserve native anchors even when a later optional model window fails.
    from openkb.evidence import Evidence, ParseStore, complete_read_bound

    reader = ParseStore(kb_dir).reader(source, parsed)
    for block in parsed.blocks:
        if block.kind != "heading" or block.id in excluded or "attachment" in block.location:
            continue
        if any(row["start_block"] == block.id for row in sections):
            continue
        view = reader.read(
            Evidence(source.source_id, source.id, parsed.id, block.id),
            max_chars=complete_read_bound(block),
        )
        title = native_title({"text": view.text})
        sections.append(
            {
                "title": title,
                "title_origin": "source",
                "level": block.location.get("heading_level", 1),
                "start_block": block.id,
                "anchor": title,
                "order": block.order,
            }
        )
    sections.sort(key=lambda row: row["order"])
    if sections:
        record["nodes"] = section_tree(source, parsed, sections)
    start = 0
    options = settings.get("navigation") or {}
    record["windows"] = []
    while start < len(parsed.blocks):
        processing_checkpoint("index_structure")
        try:
            descriptor, evidence, end, tokens = _window(
                kb_dir, source, parsed, start, sections, settings, allowance
            )
        except (IndexAllowanceExceeded, ProcessingIncomplete) as exc:
            record_optional_failure(record, exc)
            descriptor = evidence_descriptor(source, parsed, start, start + 1)
            record["windows"].append(
                {
                    "evidence": descriptor,
                    "target_start": start,
                    "target_end": start + 1,
                    "status": "basic",
                    "reason": str(exc),
                    "target_tokens": options.get("window_tokens", 200000),
                    "unlocated": [row for row in unresolved if row["start"] <= start < row["end"]],
                }
            )
            start += 1
            continue
        manifest = {
            "evidence": descriptor,
            "target_start": start,
            "target_end": end,
            "status": "complete",
            "reason": None,
            "tokens": tokens,
            "target_tokens": options.get("window_tokens", 200000),
            "limit_reason": "window_target_or_request_capacity"
            if end < len(parsed.blocks)
            else None,
            "overlap_start": descriptor["start"],
        }
        issues = [row for row in unresolved if row["start"] < end and start < row["end"]]
        if issues:
            manifest.update(status="basic", reason="index_unlocated_contents", unlocated=issues)
        blocks = {row["id"]: row for row in evidence["blocks"] if row["id"] not in excluded}
        try:
            value = (
                request_value(
                    evidence,
                    _task(start, end, [row for row in sections if row["order"] < start]),
                    SYSTEM,
                    settings,
                    bundle,
                    allowance,
                    checkpoints,
                    record["profile"],
                    validate_sections,
                )
                if any(left < end and start < right for left, right in uncovered)
                else {"sections": []}
            )
            offsets = {b.id: b.order for b in parsed.blocks[descriptor["start"] : end]}
            problems = [row for row in value["sections"] if not located(row, blocks)]
            corrected = iter(
                locate_problems(
                    problems, evidence, settings, bundle, allowance, checkpoints, record["profile"]
                )
            )
            resolved = [
                row if located(row, blocks) else next(corrected) for row in value["sections"]
            ]
            valid = [row for row in resolved if row is not None and located(row, blocks)]
            if any(
                offsets[a["start_block"]] > offsets[b["start_block"]]
                for a, b in zip(valid, valid[1:])
            ):
                raise IndexAllowanceExceeded("index_sections_out_of_order")
            if len(valid) != len(value["sections"]):
                manifest.update(status="basic", reason="index_unlocated_sections")
                record.update(status="degraded", reason="index_unlocated_sections")
            valid.sort(key=lambda row: offsets[row["start_block"]])
            for row in valid:
                order = offsets[row["start_block"]]
                if order < start:
                    continue
                candidate = {**row, "order": order}
                if not any(
                    old == candidate
                    or (
                        old["start_block"] == candidate["start_block"]
                        and old["title"] == candidate["title"]
                        and old["title_origin"] == candidate["title_origin"] == "source"
                    )
                    for old in sections
                ):
                    sections.append(candidate)
            sections.sort(key=lambda row: row["order"])
            previous = {node["id"]: node for node in record["nodes"]}
            record["nodes"] = section_tree(source, parsed, sections)
            for node in record["nodes"]:
                old = previous.get(node["id"])
                if old and old["end"] == node["end"]:
                    node.update(summary=old["summary"], summary_origin=old["summary_origin"])
            if options.get("summaries", True):
                selected = [
                    node
                    for node in record["nodes"]
                    if node["parent"] is not None
                    and start <= node["start"] < end
                    and node["end"] <= end
                ]
                if not selected and not sections:
                    selected = record["nodes"]
                if selected:
                    summarize_group(
                        evidence,
                        selected,
                        settings,
                        bundle,
                        allowance,
                        checkpoints,
                        record["profile"],
                    )
        except (IndexAllowanceExceeded, ProcessingIncomplete) as exc:
            record_optional_failure(record, exc)
            manifest.update(status="basic", reason=str(exc))
        record["windows"].append(manifest)
        start = end
        del evidence, blocks
