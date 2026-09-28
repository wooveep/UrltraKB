"""Ordered body windows yield starts; code owns hierarchy, extents and coverage."""

import re

import litellm

from openkb.agent.source_protocol import source_messages
from openkb.navigation_anchors import inherit_summaries, section_candidate
from openkb.navigation_enhancement import IndexAllowanceExceeded, record_optional_failure
from openkb.navigation_evidence import evidence_descriptor, read_evidence_group, verified_reader
from openkb.navigation_reading import (
    DEFAULT_WINDOW_TOKENS,
    ReadingBoundaries,
    possible_starts,
    reconsider_empty,
)
from openkb.navigation_requests import expand_capacity, request_value, summary_fits
from openkb.navigation_tree import validate_nodes
from openkb.processing import ProcessingIncomplete, processing_checkpoint
from openkb.sources import content_id

SYSTEM = """Analyze only the target body blocks in this window; overlap is context, not a
new target. Continue the supplied accepted section state. Native headings are reliable
anchors and must retain their original titles; a missing heading_level does not mean level 1.
Supply document-relative levels for headings in the current target, including native anchors.
Introduce inferred labels only for real
organization changes. Do not turn running headers, footers, a table of contents or ordinary
mentions into section starts. Return JSON {"sections":[{"title":"heading or label",
"title_origin":"source or inferred","level":1,"start_block":"block id","anchor":"short
verbatim excerpt within that block"}]}. Levels are 1 through 9. Return only NEW starts in
source order, without end ranges, offsets, explanations or historical tree edits. An empty
sections array is valid for continuation or when no finer structure is justified."""

SYSTEM += """ Read the whole target in source order. Physical pages, slides and rows are
reading positions, not necessarily sections. A paragraph may contain a real chapter title;
do not restrict inspection to blocks marked heading. Keep original chapters distinct from
knowledge topics: this task builds navigation, not wiki pages. For sources without headings,
use inferred labels only when the content supports a meaningful boundary; never invent
chapter numbers. Prior ancestors are context, not proof that later content belongs to them."""


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


def valid_section(row):
    return (
        isinstance(row, dict)
        and set(row) == {"title", "title_origin", "level", "start_block", "anchor"}
        and isinstance(row["title"], str)
        and 0 < len(row["title"]) <= 320
        and isinstance(row["title_origin"], str)
        and row["title_origin"] in {"source", "inferred"}
        and type(row["level"]) is int
        and 1 <= row["level"] <= 9
        and isinstance(row["start_block"], str)
        and isinstance(row["anchor"], str)
        and 0 < len(row["anchor"]) <= 320
    )


def located(row, blocks):
    block = blocks.get(row["start_block"])
    if block is None or block["kind"] in {"metadata", "image"}:
        return False
    if block["location"].get("role") in {"toc", "header", "footer"}:
        return False
    anchor = normalized(row["anchor"])
    if not anchor or anchor not in normalized(block["text"]):
        return False
    if row["title_origin"] == "inferred":
        return True
    # Layout/OCR may separate a number and title with a newline or omit a
    # space. Compare complete titles at the supplied block, not substrings.
    title = "".join(row["title"].split())
    if block["kind"] == "heading":
        return title == "".join(native_title(block).split())
    if title == "".join(block["text"].split()):
        return True
    return any(
        title == "".join(line.split()) for line in block["text"].splitlines() if line.strip()
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
        level = row["level"]
        while stack and (level is None or stack[-1][0] >= level):
            stack.pop()
        next_order = next(
            (
                s["order"]
                for s in sections[i + 1 :]
                if level is None or s["level"] is None or s["level"] <= level
            ),
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
        if "level_origin" in row:
            node["structure"] = {
                "level": level,
                "level_origin": row["level_origin"],
                "anchors": row["anchors"],
            }
        nodes.append(node)
        if level is not None:
            stack.append((level, node["id"]))
    by_id = {node["id"]: node for node in nodes}
    for node in reversed(nodes[1:]):
        parent = by_id[node["parent"]]
        parent["end"] = max(parent["end"], node["end"])
    validate_nodes(nodes, len(parsed.blocks))
    return nodes


def _task(start, end, sections):
    ancestors = []
    for row in sections:
        if row["level"] is None:
            ancestors.clear()
            continue
        while ancestors and ancestors[-1]["level"] >= row["level"]:
            ancestors.pop()
        ancestors.append(row)
    return {
        "stage": "index_structure",
        "target": {"start": start, "end": end},
        "continuation": {"version": content_id(sections), "sections": ancestors},
    }


def _window(
    kb,
    source,
    parsed,
    start,
    sections,
    settings,
    allowance,
    *,
    stop=None,
    reader=None,
    unresolved=(),
    boundaries=None,
):
    reader = verified_reader(kb, source, parsed, reader)
    boundaries = boundaries or ReadingBoundaries(parsed)
    target = (settings.get("navigation") or {}).get("window_tokens", DEFAULT_WINDOW_TOKENS)
    accepted = [row for row in sections if row["order"] < start]

    def candidate(overlap, end, *, summary_budget=True):
        descriptor = evidence_descriptor(source, parsed, overlap, end)
        evidence = read_evidence_group(kb, source, parsed, descriptor, reader=reader)
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
        if fits and summary_budget and (settings.get("navigation") or {}).get("summaries", True):
            expected = {
                "title": max((b["text"][:320] for b in evidence["blocks"]), key=len, default=""),
                "title_origin": "inferred",
                "start": start,
                "end": end,
            }
            from openkb.navigation_metadata import structure_diagnostics

            diagnostics = structure_diagnostics(
                {},
                [
                    {
                        "target_start": start,
                        "target_end": end,
                        "unlocated": [
                            r for r in unresolved if r["start"] < end and start < r["end"]
                        ],
                    }
                ],
            )
            fits = summary_fits(
                evidence, [expected], settings, allowance, structure_issues=diagnostics
            )
        return (descriptor, evidence, end, tokens) if fits else None

    overlap = boundaries.overlap(start)
    chosen = candidate(overlap, start + 1)
    if chosen is not None and overlap < start - 1 and chosen[3] > target // 3:
        # An unusually large previous unit must not crowd out the new target.
        overlap = start - 1
        chosen = candidate(overlap, start + 1)
    if chosen is None and overlap < start:
        overlap = max(0, start - 1)
        chosen = candidate(overlap, start + 1)
    if chosen is None and overlap < start:
        overlap = start
        chosen = candidate(overlap, start + 1)
    if chosen is None:
        chosen = candidate(start, start + 1, summary_budget=False)
        overlap = start
    if chosen is None:
        raise IndexAllowanceExceeded("index_indivisible_range_exceeds_window")
    # Exponential growth and bisection avoid reserializing the window once
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
    # A caller's explicit lookup range may intentionally end at a heading.
    aligned = chosen[2] if stop == chosen[2] else boundaries.aligned_end(start, chosen[2])
    if aligned != chosen[2]:
        chosen = candidate(overlap, aligned) or chosen
    return chosen


def infer_missing(
    kb_dir, source, parsed, record, settings, bundle, allowance, checkpoints, *, reader=None
):
    from openkb.navigation_toc import directory_sections

    reader = verified_reader(kb_dir, source, parsed, reader)
    from openkb.navigation_anchors import AnchorLookup

    anchors = AnchorLookup(source, parsed, reader)
    boundaries = ReadingBoundaries(parsed)
    sections, excluded, uncovered, unresolved = [], set(), [(0, len(parsed.blocks))], []
    try:
        sections, excluded, uncovered, unresolved = directory_sections(
            kb_dir,
            source,
            parsed,
            settings,
            bundle,
            allowance,
            checkpoints,
            record["profile"],
            reader=reader,
        )
    except (IndexAllowanceExceeded, ProcessingIncomplete) as exc:
        record_optional_failure(record, exc)
    if unresolved:
        record.update(status="degraded", reason="index_unlocated_contents")
    mapped, sections = sections, []
    for row in mapped:
        anchors.merge(sections, section_candidate(row, parsed, "toc"))
    # Preserve native anchors even when a later optional model window fails.
    from openkb.evidence import Evidence, complete_read_bound

    for block in parsed.blocks:
        if (
            block.kind != "heading"
            or block.id in excluded
            or "attachment" in block.location
            or block.location.get("role") in {"toc", "header", "footer"}
        ):
            continue
        view = reader.read(
            Evidence(source.source_id, source.id, parsed.id, block.id),
            max_chars=complete_read_bound(block),
        )
        title = native_title({"text": view.text})
        if not title:
            continue
        anchors.merge(
            sections,
            section_candidate(
                {
                    "title": title,
                    "title_origin": "source",
                    "level": None,
                    "start_block": block.id,
                    "anchor": title,
                    "order": block.order,
                },
                parsed,
                "unknown",
            ),
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
                kb_dir,
                source,
                parsed,
                start,
                sections,
                settings,
                allowance,
                reader=reader,
                unresolved=unresolved,
                boundaries=boundaries,
            )
        except (IndexAllowanceExceeded, ProcessingIncomplete) as exc:
            record_optional_failure(record, exc)
            # A spent optional allowance must not turn the remaining document
            # into one artificial navigation group per block.  Preserve one
            # contiguous degraded W; DocumentPlan later splits its movable T
            # with the selected model's actual capacity before any request.
            end = len(parsed.blocks)
            descriptor = evidence_descriptor(source, parsed, start, end)
            record["windows"].append(
                {
                    "evidence": descriptor,
                    "target_start": start,
                    "target_end": end,
                    "status": "basic",
                    "reason": str(exc),
                    "target_tokens": options.get("window_tokens", DEFAULT_WINDOW_TOKENS),
                    "unlocated": [
                        row for row in unresolved if row["start"] < end and start < row["end"]
                    ],
                }
            )
            break
        manifest = {
            "evidence": descriptor,
            "target_start": start,
            "target_end": end,
            "status": "complete",
            "reason": None,
            "tokens": tokens,
            "target_tokens": options.get("window_tokens", DEFAULT_WINDOW_TOKENS),
            "limit_reason": "window_target_or_request_capacity"
            if end < len(parsed.blocks)
            else None,
            "overlap_start": descriptor["start"],
        }
        issues = [row for row in unresolved if row["start"] < end and start < row["end"]]
        if issues:
            manifest.update(status="basic", reason="index_unlocated_contents", unlocated=issues)
        blocks = {row["id"]: row for row in evidence["blocks"] if row["id"] not in excluded}
        cues = possible_starts({"blocks": list(blocks.values())}, start, end)
        needs_structure = any(left < end and start < right for left, right in uncovered)
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
                    reconsider=lambda value: reconsider_empty(value, cues),
                )
                if needs_structure
                else {"sections": []}
            )
            if needs_structure and reconsider_empty(value, cues):
                # No model-confirmed starts after bounded review. Keep usable navigation,
                # but expose uncertainty to summaries and the global planner.
                manifest.update(status="basic", reason="index_structure_unconfirmed")
                manifest["structure_issues"] = {
                    "reason": "index_structure_unconfirmed",
                    "count": len(cues),
                    "candidates": cues,
                }
                record.update(status="degraded", reason="index_structure_unconfirmed")
            offsets = {b.id: b.order for b in parsed.blocks[descriptor["start"] : end]}
            rows = [row for row in value["sections"] if valid_section(row)]
            problems = [row for row in rows if not located(row, blocks)]
            try:
                corrections = locate_problems(
                    problems, evidence, settings, bundle, allowance, checkpoints, record["profile"]
                )
            except (IndexAllowanceExceeded, ProcessingIncomplete) as exc:
                record_optional_failure(record, exc)
                corrections = [None] * len(problems)
            corrected = iter(corrections)
            resolved = [row if located(row, blocks) else next(corrected) for row in rows]
            valid = [row for row in resolved if row is not None and located(row, blocks)]
            if len(valid) != len(value["sections"]):
                manifest.update(status="basic", reason="index_unlocated_sections")
                manifest["structure_issues"] = {
                    "reason": "index_unlocated_sections",
                    "count": len(value["sections"]) - len(valid),
                    "candidates": [
                        {
                            "title": str(row.get("title", ""))[:320],
                            "start_block": str(row.get("start_block", ""))[:128],
                        }
                        for row in value["sections"]
                        if isinstance(row, dict) and row not in valid
                    ],
                }
                record.update(status="degraded", reason="index_unlocated_sections")
            valid.sort(key=lambda row: offsets[row["start_block"]])
            for row in valid:
                order = offsets[row["start_block"]]
                if order < start:
                    continue
                candidate = section_candidate({**row, "order": order}, parsed, "model")
                anchors.merge(sections, candidate)
            sections.sort(key=lambda row: row["order"])
            if sections:
                nodes = section_tree(source, parsed, sections)
                inherit_summaries(nodes, record["nodes"])
                record["nodes"] = nodes
        except (IndexAllowanceExceeded, ProcessingIncomplete) as exc:
            record_optional_failure(record, exc)
            manifest.update(status="basic", reason=str(exc))
        if options.get("summaries", True):
            from openkb.navigation_summaries import summarize_pending

            summarize_pending(
                kb_dir,
                source,
                parsed,
                record,
                evidence,
                end,
                settings,
                bundle,
                allowance,
                checkpoints,
                manifest,
                reader=reader,
            )
        record["windows"].append(manifest)
        start = end
        del evidence, blocks
    from openkb.navigation_summaries import finish_summary_states

    finish_summary_states(record["nodes"], options.get("summaries", True))
