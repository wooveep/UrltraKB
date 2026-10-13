"""Bounded source scopes based on literal headings, never generated navigation."""

import re

from openkb.source_qualification import literal_spans as _literal_spans
from openkb.source_qualification import markup_headings as _markup_headings
from openkb.source_qualification import markup_source, qualified_headings
from openkb.source_reading import reading_action

_HEADING = re.compile(r"(?m)^(?:(#{1,6})[ \t]+|([0-9]+(?:\.[0-9]+)*)(?:\.)?[ \t]+)([^\n]+)$")
_UNCERTAIN_HEADING = re.compile(
    r"(?m)^(?:[0-9]+(?:\.[0-9]+)*\.?(?=[^\W\d_])[^\n]+"
    r"|[0-9]+(?:\.[0-9]+)+\.?[ \t]*\r?)$"
)


def _units(raw, part=None):
    if isinstance(raw, list):
        return [(p["page"], p["parts"][part] if part else p.get("content", "")) for p in raw]
    return [(1, raw["text"] if isinstance(raw, dict) else raw)]


def _bounds(raw, location, selected):
    from pageindex.index.utils import parse_pages

    if selected is not None:
        if isinstance(raw, list):
            if "page_spans" in selected:
                return [(p["page"], p["start"], p["end"]) for p in selected["page_spans"]]
            return [(p["page"], 0, len(p["content"])) for p in selected.get("units", [])]
        return [(1, a, b) for a, b in selected.get("source_spans", [])]
    if isinstance(raw, list):
        requested = parse_pages(str(location.get("pages", location.get("slides", "1"))))
        return [(n, 0, len(t)) for n, t in _units(raw) if n in requested]
    text = raw["text"] if isinstance(raw, dict) else raw
    if "blocks" in location and isinstance(raw, dict):
        requested = parse_pages(str(location["blocks"]))
        return [
            (1, a, b)
            for block in raw["blocks"]
            if block["ordinal"] in requested
            for a, b in block["source_spans"]
        ]
    if "chars" in location:
        a, b = str(location["chars"]).split(":")
        return [(1, int(a), int(b))]
    return [(1, 0, len(text))]


def _source_headings(raw, units) -> list[dict]:
    """Separate recorded markup structure from ambiguous native text boundaries."""
    policy = raw.get("normalization_policy") if isinstance(raw, dict) else None
    if markup_source(policy):
        text = raw["text"]
        confirmed = set(qualified_headings(text, policy, raw.get("origins", [])))
        return [
            {
                "unit": 1,
                "offset": start,
                "text": title,
                "literal": text[start:end].rstrip("\r\n"),
                "level": level if (start, end, level, title) in confirmed else None,
            }
            for start, end, level, title in _markup_headings(text)
        ]

    # A page, slide, TXT, cell or legacy string does not attest Markdown syntax.
    # Numbers/symbols can be chapters, commands, versions, quantities or examples.
    # Keep candidate boundaries and native offsets without asserting any ancestry.
    literals = _literal_spans("\n".join(body for _, body in units))
    headings, base = [], 0
    for number, body in units:
        matches = {m.start(): m[0] for m in _HEADING.finditer(body)}
        matches.update((m.start(), m[0]) for m in _UNCERTAIN_HEADING.finditer(body))
        for offset, literal in sorted(matches.items()):
            if any(start <= base + offset < end for start, end in literals):
                continue
            if len(literal) > 180 or re.search(r"\.{3,}|…{2,}", literal):
                continue
            headings.append(
                {
                    "unit": number,
                    "offset": offset,
                    "text": literal,
                    "literal": literal,
                    "level": None,
                }
            )
        base += len(body) + 1
    return headings


def original_context(
    source: dict, raw, location: dict, *, match_offset: int | None = None, selected=None
) -> dict:
    """Report source spans and their evidenced heading path, with executable neighbors."""
    units = _units(raw, location.get("part"))
    headings = _source_headings(raw, units)
    stack: list[dict] = []
    for h in headings:
        if h["level"] is None:
            # Unknown source structure must not acquire a parent from an earlier
            # regex match, including across physical pages or syntax families.
            stack.clear()
            h["path"] = []
            continue
        while stack and stack[-1]["level"] >= h["level"]:
            stack.pop()
        stack.append({k: v for k, v in h.items() if k != "path"})
        # Keep ancestry checks internal; expose literal source positions only.
        h["path"] = [{k: v for k, v in item.items() if k != "level"} for item in stack]
    regions, actions = [], []
    bodies = dict(units)
    for n, start, end in _bounds(raw, location, selected):
        if match_offset is not None:
            start = min(end, start + match_offset)
            end = min(end, start + 1)
        positions = [
            start,
            *(h["offset"] for h in headings if h["unit"] == n and start < h["offset"] < end),
        ]
        for position in positions:
            before = [h for h in headings if (h["unit"], h["offset"]) <= (n, position)]
            following = next(
                (h["offset"] for h in headings if h["unit"] == n and h["offset"] > position),
                len(bodies[n]),
            )
            lower = before[-1]["offset"] if before and before[-1]["unit"] == n else 0
            if "cells" in location:
                lower, following = start, end
            regions.append(
                {
                    "unit": n,
                    "char_range": [position, min(end, following)],
                    "status": "source_heading" if before and before[-1]["path"] else "unconfirmed",
                    "headings": before[-1]["path"] if before else [],
                    **(
                        {
                            "unconfirmed_boundary": {
                                key: before[-1][key] for key in ("unit", "offset", "literal")
                            }
                        }
                        if before and not before[-1]["path"]
                        else {}
                    ),
                    "nearby_original": bodies[n][
                        max(lower, position - 200) : min(following, position + 600)
                    ],
                }
            )
        if isinstance(raw, list):
            for neighbor in (n - 1, n + 1):
                if neighbor in bodies:
                    actions.append(reading_action(source, {"pages": str(neighbor)}))
        else:
            actions.append(
                reading_action(
                    source, {"chars": f"{max(0, start - 1200)}:{min(len(bodies[n]), end + 1200)}"}
                )
            )
    return {
        "source_path": source["path"],
        "source_revision_id": source["source_revision_id"],
        "content_digest": source["content_digest"],
        **{
            key: source[key]
            for key in ("processing_status", "target_source_revision_id", "interpretation")
            if key in source
        },
        "native_location": location,
        "regions": regions[:8],
        "regions_omitted": max(0, len(regions) - 8),
        "adjacent_actions": actions[:4],
        "reading_action": reading_action(source, location),
        "scope_rule": "Heading ancestry requires recorded source structure. Unconfirmed "
        "text boundaries do not mean missing original content; use the unchanged text "
        "and native locations without assuming a parent chapter.",
    }
