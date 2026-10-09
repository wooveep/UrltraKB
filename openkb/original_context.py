"""Bounded source scopes based on literal headings, never generated navigation."""

import re

from openkb.source_reading import reading_action

_HEADING = re.compile(r"(?m)^(?:(#{1,6})[ \t]+|([0-9]+(?:\.[0-9]+)*)(?:\.)?[ \t]+)([^\n]+)$")


def _units(raw):
    if isinstance(raw, list):
        return [(p["page"], p.get("content", "")) for p in raw]
    return [(1, raw["text"] if isinstance(raw, dict) else raw)]


def _bounds(source, raw, location):
    from pageindex.index.utils import parse_pages

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


def original_context(source: dict, raw, location: dict, *, match_offset: int | None = None) -> dict:
    """Report source spans and their evidenced heading path, with executable neighbors."""
    units = _units(raw)
    headings = []
    for number, body in units:
        for m in _HEADING.finditer(body):
            title = m[3].strip().rstrip("#").strip()
            if len(title) > 180 or re.search(r"\.{3,}|…{2,}", title):
                continue
            headings.append(
                {
                    "unit": number,
                    "offset": m.start(),
                    "text": title,
                    "literal": m[0],
                    "level": len(m[1]) if m[1] else len(m[2].split(".")),
                }
            )
    # Repeated running headers are not reliable section boundaries.
    repeated = {
        h["literal"]
        for h in headings
        if len({v["unit"] for v in headings if v["literal"] == h["literal"]}) > 1
    }
    headings = [h for h in headings if h["literal"] not in repeated]
    stack: list[dict] = []
    for h in headings:
        while stack and stack[-1]["level"] >= h["level"]:
            stack.pop()
        stack.append({k: v for k, v in h.items() if k != "level"})
        # Keep levels internally while returning only verified source positions.
        stack[-1]["level"] = h["level"]
        h["path"] = [{k: v for k, v in item.items() if k != "level"} for item in stack]
    regions, actions = [], []
    bodies = dict(units)
    for n, start, end in _bounds(source, raw, location):
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
            regions.append(
                {
                    "unit": n,
                    "char_range": [position, min(end, following)],
                    "status": "source_heading" if before else "unconfirmed",
                    "headings": before[-1]["path"] if before else [],
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
        "native_location": location,
        "regions": regions[:8],
        "regions_omitted": max(0, len(regions) - 8),
        "adjacent_actions": actions[:4],
        "reading_action": reading_action(source, location),
        "scope_rule": "Keep each heading's object, operation and conditions together; "
        "unconfirmed ancestry is unknown.",
    }
