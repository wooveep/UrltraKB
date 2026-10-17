"""Native parent bounds and explicit gaps for contradictory original numbering.

Model numbers are navigation proposals. Boundaries here only narrow literal
searches; heading roles still require delivered verification before publication.
"""

import copy
import re
from functools import lru_cache

from ..errors import IndexQualityError
from .physical_sections import (
    SectionBoundary,
    compact,
    heading,
    literal_heading_matches,
    literal_title_pattern,
    unique_heading,
)


def position(anchor):
    return anchor["unit"], anchor["range"][0]


@lru_cache(maxsize=8)
def _native_numbers(texts, start):
    result = []
    for number, text in enumerate(texts, start):
        lines, offset = text.splitlines(keepends=True), 0
        for index, line in enumerate(lines):
            parsed = heading(line, allow_attached=True)
            section = parsed[1] if parsed else None
            if (
                section is None
                and re.fullmatch(r"\d+(?:\.\d+)*", line.strip())
                and index + 1 < len(lines)
            ):
                section = line.strip()
            if section:
                result.append((tuple(int(n) for n in section.split(".")), (number, offset)))
            offset += len(line)
    return tuple(result)


def native_parent_boundary(item, anchor, pages, start, upper):
    """Narrow a search at the next native sibling, even before it is generated.

    Earlier section numbers in operation lists cannot restart a parent. These
    bounds constrain search only; they do not confer a heading-role verdict.
    """
    lower, section = position(anchor), item.get("structure")
    if isinstance(section, str) and re.fullmatch(r"\d+(?:\.\d+)*", section):
        number = tuple(int(n) for n in section.split("."))
        for other, point in _native_numbers(tuple(p[0] for p in pages), start):
            if (
                lower < point < upper
                and other[0] == number[0]
                and len(other) <= len(number)
                and other > number[: len(other)]
            ):
                upper = point
                break
    return SectionBoundary(item["title"], lower, upper, structure=section)


def conflicting_number(item, pages, start, boundary):
    section = item.get("structure")
    if not section:
        return None
    parsed = heading(item["title"], allow_attached=True)
    wanted = compact(parsed[0] if parsed else item["title"])
    found = []
    for number in range(
        max(start, boundary.start[0]), min(start + len(pages), boundary.end[0] + 1)
    ):
        text = pages[number - start][0]
        lines, offset = text.splitlines(keepends=True), 0
        for index, line in enumerate(lines[:-1]):
            if line.strip() == section:
                other = heading(lines[index + 1], allow_attached=True)
                if other and other[1] and other[1] != section and compact(other[0]) == wanted:
                    end = offset + len(line) + len(lines[index + 1].rstrip())
                    anchor = {"unit": number, "range": [offset, end], "excerpt": text[offset:end]}
                    if boundary.contains(anchor):
                        found.append(
                            {
                                **anchor,
                                "section": section,
                                "title": item["title"],
                                "reason": "conflicting_original_numbers",
                            }
                        )
            offset += len(line)
    return found[0] if len(found) == 1 else None


def locate_body_entries(entries, pages, start):
    """Return unchanged-wording proposals paired with parent-constrained anchors."""
    items = copy.deepcopy(entries)
    for item in items:
        gaps = item.get("navigation_gaps", [])
        if not isinstance(gaps, list) or any(not isinstance(gap, dict) for gap in gaps):
            raise IndexQualityError("Original numbering diagnostics must be a list of source spans")

    def locate(item, boundary=None):
        anchor = unique_heading(item, pages, start, prefer_declared_page=True, boundary=boundary)
        if anchor:
            return anchor
        parsed = heading(item["title"], allow_attached=True)
        section = item.get("structure")
        if parsed and parsed[1] and section and parsed[1] != section:
            return None
        # Plain PDF title lines have no Markdown syntax. Locate complete native
        # lines within the same parent interval; a later role verdict is still
        # required, just as for numbered and Markdown headings.
        candidates = []
        lower = max(start, boundary.start[0]) if boundary else start
        upper = min(start + len(pages), boundary.end[0] + 1) if boundary else start + len(pages)
        for number in range(lower, upper):
            for match in literal_heading_matches(
                literal_title_pattern(item), pages[number - start][0]
            ):
                current = {"unit": number, **match, "evidence": "text_match"}
                if (not section or not match["section"] or section == match["section"]) and (
                    boundary is None or boundary.contains(current)
                ):
                    candidates.append(current)
        local = [a for a in candidates if a["unit"] == item.get("physical_index")]
        candidates = local or candidates
        return candidates[0] if len(candidates) == 1 else None

    anchors = [locate(item) for item in items]
    indices = {}
    for index, item in enumerate(items):
        if item.get("structure"):
            indices.setdefault(item["structure"], index)
    outer = SectionBoundary("body recovery", (start, 0), (start + len(pages), 0))
    bounds, parents = {}, {}

    def parent(index):
        section = items[index].get("structure")
        if not isinstance(section, str):
            return None
        while "." in section:
            section = section.rsplit(".", 1)[0]
            if section in indices:
                return indices[section]
        return None

    def child_bounds(index):
        if index in bounds:
            return bounds[index]
        ancestor = parent(index)
        parents[index] = ancestor
        if ancestor is None:
            bounds[index] = outer
            return outer
        enclosing = child_bounds(ancestor)
        root = anchors[ancestor]
        if root is None or not enclosing.contains(root):
            root = locate(items[ancestor], enclosing)
        anchors[ancestor] = root
        if root is None:
            bounds[index] = enclosing
            return enclosing
        section = items[ancestor]["structure"]
        depth = len(section.split("."))
        end = enclosing.end
        for other, anchor in enumerate(anchors):
            other_section = items[other].get("structure")
            if not anchor or not isinstance(other_section, str) or other == ancestor:
                continue
            if len(other_section.split(".")) <= depth and not other_section.startswith(
                section + "."
            ):
                point = position(anchor)
                if position(root) < point < end:
                    end = point
        bounds[index] = native_parent_boundary(items[ancestor], root, pages, start, end)
        return bounds[index]

    omitted = set()
    for index, item in enumerate(items):
        boundary = child_bounds(index)
        if boundary != outer and (anchors[index] is None or not boundary.contains(anchors[index])):
            anchors[index] = locate(item, boundary)
        ancestor = parents.get(index)
        # Preserve both contradictory original labels as a diagnostic. A
        # validated literal parent covers the original text; no child number
        # or heading-role verdict is invented to force the import through.
        if anchors[index] is None and ancestor is not None and anchors[ancestor] is not None:
            gap = conflicting_number(item, pages, start, boundary)
            if gap:
                gaps = items[ancestor].setdefault("navigation_gaps", [])
                if gap not in gaps:
                    gaps.append(gap)
                omitted.add(index)
    for index, item in enumerate(items):
        gaps = item.get("navigation_gaps", [])
        if not isinstance(gaps, list) or any(not isinstance(gap, dict) for gap in gaps):
            raise IndexQualityError("Original numbering diagnostics must be a list of source spans")
        for gap in gaps:
            unit, span = gap.get("unit"), gap.get("range")
            if (
                type(unit) is not int
                or not start <= unit < start + len(pages)
                or not isinstance(span, list)
                or len(span) != 2
                or any(type(n) is not int for n in span)
                or not 0 <= span[0] < span[1] <= len(pages[unit - start][0])
                or pages[unit - start][0][slice(*span)] != gap.get("excerpt")
                or anchors[index] is None
                or position(anchors[index]) >= (unit, span[0])
                or not isinstance(gap.get("section"), str)
                or not isinstance(gap.get("title"), str)
            ):
                raise IndexQualityError(
                    "Original numbering diagnostic is not covered by its native parent"
                )
            boundary = native_parent_boundary(
                item, anchors[index], pages, start, child_bounds(index).end
            )
            section = item.get("structure")
            if (
                not isinstance(section, str)
                or not gap.get("section", "").startswith(section + ".")
                or conflicting_number(
                    {"structure": gap.get("section"), "title": gap.get("title", "")},
                    pages,
                    start,
                    boundary,
                )
                != gap
            ):
                raise IndexQualityError(
                    "Original numbering diagnostic is not covered by its native parent"
                )
    return [(item, anchors[index]) for index, item in enumerate(items) if index not in omitted]
