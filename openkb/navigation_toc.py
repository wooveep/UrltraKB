"""Read declared contents once and resolve unambiguous body headings in code."""

import re

from openkb.evidence import Evidence, ParseStore, complete_read_bound
from openkb.navigation_enhancement import IndexAllowanceExceeded
from openkb.navigation_requests import request_value
from openkb.processing import processing_checkpoint
from openkb.resource_budget import check_memory

_CONTENTS = {"contents", "table of contents", "toc", "目录", "目次"}


def _entry(line, block):
    link = re.fullmatch(r"(?:[-*+]\s+)?\[([^\]]+)\]\(#[^)]+\)", line)
    numbered = re.fullmatch(r"(.+?)(?:\s*\.{2,}\s*|\t+|\s{2,})([0-9]+|[ivxlcdm]+)", line, re.I)
    match = link or numbered
    if match or block.location.get("role") == "toc":
        return {
            "title": match[1] if match else line,
            "level": block.location.get("toc_level", 1),
            "page_label": numbered[2] if numbered else None,
            "toc_block": block.id,
            "anchor": line,
        }
    return None


def _scan(source, parsed, reader):
    """Stream source text; retain directory entries and positions, never PDF bodies."""
    from openkb.navigation_structure import native_title, normalized

    entries, excluded, ambiguous = [], set(), []
    in_toc, toc_page, ambiguous_blocks = False, None, 0
    for block in parsed.blocks:
        processing_checkpoint()
        check_memory(complete_read_bound(block) * 12, stage="index_structure")
        view = reader.read(
            Evidence(source.source_id, source.id, parsed.id, block.id),
            max_chars=complete_read_bound(block),
        )
        lines = [line.strip() for line in view.text.splitlines() if line.strip()]
        starts_contents = lines and native_title({"text": lines[0]}).casefold() in _CONTENTS
        if starts_contents:
            in_toc, toc_page, ambiguous_blocks = True, block.location.get("page"), 0
            excluded.add(block.id)
            lines = lines[1:]
        declared = block.location.get("role") == "toc"
        if block.kind == "heading" and not declared and not starts_contents:
            in_toc = False
        if not in_toc and not declared:
            continue
        recognized = [_entry(line, block) for line in lines]
        uncertain = any(row is None for row in recognized)
        if (
            uncertain
            and not declared
            and lines
            and normalized(lines[0]) in {normalized(entry["title"]) for entry in entries}
        ):
            in_toc = False
            continue
        # PDF bodies have paragraph kinds. A page transition plus non-contents
        # text ends the directory even when no native heading kind is available.
        if uncertain and not declared and not starts_contents:
            page = block.location.get("page")
            if (page is not None and page != toc_page) or (page is None and ambiguous_blocks):
                in_toc = False
                continue
            ambiguous_blocks += 1
        if uncertain:
            ambiguous.append(block.order)
        else:
            excluded.add(block.id)
        entries.extend(row for row in recognized if row is not None)
    return entries, excluded, ambiguous


def _extract(evidence, settings, bundle, allowance, checkpoints, profile):
    available = {row["id"]: row for row in evidence["blocks"]}

    def validate(value):
        if (
            not isinstance(value, dict)
            or set(value) != {"entries"}
            or not isinstance(value["entries"], list)
        ):
            raise ValueError("Invalid contents")
        for item in value["entries"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"title", "level", "page_label", "toc_block", "anchor"}
                or not isinstance(item["title"], str)
                or not 0 < len(item["title"]) <= 320
                or type(item["level"]) is not int
                or not 1 <= item["level"] <= 9
                or (item["page_label"] is not None and not isinstance(item["page_label"], str))
                or not isinstance(item["toc_block"], str)
                or item["toc_block"] not in available
                or not isinstance(item["anchor"], str)
                or not item["anchor"]
                or item["anchor"] not in available[item["toc_block"]]["text"]
            ):
                raise ValueError("Invalid contents entry")

    return request_value(
        evidence,
        {"stage": "index_toc"},
        'Extract only declared contents entries. Return {"entries":['
        '{"title":"original title","level":1,"page_label":null,'
        '"toc_block":"block id","anchor":"short original excerpt"}]}.'
        " Keep printed page labels verbatim, including Roman numerals; "
        "use null when absent. Do not invent missing entries.",
        settings,
        bundle,
        allowance,
        checkpoints,
        profile,
        validate,
    )["entries"]


def _matches(source, parsed, reader, entries, excluded):
    from openkb.navigation_structure import native_title, normalized

    matches = {normalized(entry["title"]): [] for entry in entries}
    for block in parsed.blocks:
        if block.id in excluded or (block.kind != "heading" and block.location["kind"] != "pdf"):
            continue
        processing_checkpoint()
        check_memory(complete_read_bound(block) * 12, stage="index_structure")
        view = reader.read(
            Evidence(source.source_id, source.id, parsed.id, block.id),
            max_chars=complete_read_bound(block),
        )
        titles = {native_title({"text": view.text})}
        if block.location["kind"] == "pdf":
            titles.update(normalized(line) for line in view.text.splitlines())
        for title in matches.keys() & titles:
            matches[title].append(
                {"id": block.id, "order": block.order, "page": block.location.get("page")}
            )
    return matches


def directory_sections(kb, source, parsed, settings, bundle, allowance, checkpoints, profile):
    from openkb.navigation_structure import _window, locate_problems, located, normalized

    reader = ParseStore(kb).reader(source, parsed)
    entries, excluded, ambiguous = _scan(source, parsed, reader)
    trusted = [
        {
            "title": row["title"],
            "level": row["level"],
            "page_label": None,
            "physical_page": row["physical_page"],
            "toc_block": None,
            "anchor": row["title"],
        }
        for row in parsed.profile.get("toc", [])
    ]
    if trusted:
        entries, ambiguous = trusted, []
    # Each contents region uses the same bounded evidence assembly as body windows.
    pending = set(ambiguous)
    while pending:
        start = min(pending)
        _, evidence, end, _ = _window(
            kb, source, parsed, start, [], settings, allowance, stop=max(pending) + 1
        )
        extracted = _extract(evidence, settings, bundle, allowance, checkpoints, profile)
        excluded.update(row["toc_block"] for row in extracted)
        entries.extend(
            row
            for row in extracted
            if not any(
                old["toc_block"] == row["toc_block"] and old["anchor"] == row["anchor"]
                for old in entries
            )
        )
        pending.difference_update(range(start, end))
    if not entries:
        return [], excluded, [(0, len(parsed.blocks))], []
    orders = {block.id: block.order for block in parsed.blocks}
    entries.sort(key=lambda entry: orders.get(entry["toc_block"], -1))
    matches_by_title = _matches(source, parsed, reader, entries, excluded)
    slots, previous = [], -1
    for entry in entries:
        physical = entry.get("physical_page")
        if physical is None and entry["page_label"] is not None:
            labels = parsed.profile.get("page_labels", {})
            pages = [int(page) for page, label in labels.items() if label == entry["page_label"]]
            physical = pages[0] if len(pages) == 1 else None
        matches = [
            row
            for row in matches_by_title[normalized(entry["title"])]
            if row["order"] >= previous and (physical is None or row["page"] == physical)
        ]
        if len(matches) == 1:
            row = matches[0]
            previous = row["order"]
            slots.append(
                {
                    "title": entry["title"],
                    "title_origin": "source",
                    "level": entry["level"],
                    "start_block": row["id"],
                    "anchor": entry["title"],
                    "order": row["order"],
                }
            )
        else:
            slots.append(None)
    uncovered = []
    body_start = max((orders[bid] + 1 for bid in excluded), default=0)
    groups, ranges = {}, {}
    for index, slot in enumerate(slots):
        if slot is not None:
            continue
        left = next(
            (item["order"] for item in reversed(slots[:index]) if item is not None), body_start
        )
        right = next(
            (item["order"] for item in slots[index + 1 :] if item is not None), len(parsed.blocks)
        )
        left = min(left, len(parsed.blocks) - 1)
        right = max(left + 1, right)
        ranges[index] = (left, right)
        groups.setdefault((left, right), []).append(index)
    for (left, right), indices in groups.items():
        _, evidence, end, _ = _window(kb, source, parsed, left, [], settings, allowance, stop=right)
        if end < right:
            # No bounded local correction can cover this ambiguity; the relevant
            # sequential body windows will supply starts instead.
            uncovered.append((left, right))
            continue
        blocks = {row["id"]: row for row in evidence["blocks"] if row["id"] not in excluded}
        problems = [
            {
                "title": entries[i]["title"],
                "title_origin": "source",
                "level": entries[i]["level"],
                "start_block": "unlocated",
                "anchor": entries[i]["title"],
                "toc_entry": i,
                "page_label": entries[i]["page_label"],
            }
            for i in indices
        ]
        fixed = locate_problems(
            problems, evidence, settings, bundle, allowance, checkpoints, profile
        )
        for index, row in zip(indices, fixed):
            if (
                row is not None
                and located(row, blocks)
                and left <= orders[row["start_block"]] < right
            ):
                slots[index] = {**row, "order": orders[row["start_block"]]}
        if any(slots[i] is None for i in indices):
            uncovered.append((left, right))
    mapped = [row for row in slots if row is not None]
    if not mapped and not uncovered:
        uncovered = [(0, len(parsed.blocks))]
    if any(a["order"] > b["order"] for a, b in zip(mapped, mapped[1:])):
        raise IndexAllowanceExceeded("index_contents_out_of_order")
    unresolved = [
        {
            "entry": i,
            "title": entries[i]["title"],
            "level": entries[i]["level"],
            "toc_block": entries[i]["toc_block"],
            "start": ranges[i][0],
            "end": ranges[i][1],
            "reason": "index_unlocated_contents",
        }
        for i, slot in enumerate(slots)
        if slot is None
    ]
    return mapped, excluded, uncovered, unresolved
