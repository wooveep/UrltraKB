"""Bounded source scopes based on literal headings, never generated navigation."""

import re
from functools import lru_cache

from openkb.source_reading import reading_action

_HEADING = re.compile(r"(?m)^(?:(#{1,6})[ \t]+|([0-9]+(?:\.[0-9]+)*)(?:\.)?[ \t]+)([^\n]+)$")
_UNCERTAIN_HEADING = re.compile(r"(?m)^[0-9]+(?:\.[0-9]+)*\.?(?=[^\W\d_])[^\n]+$")


@lru_cache(maxsize=8)
def _literal_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Cache only exact frozen text; line maps retain original character offsets."""
    from openkb.content_blocks import markdown_layout

    tokens, offsets = markdown_layout(text, locate_html=True)
    spans = [
        (offsets[token.map[0]], offsets[token.map[1]])
        for token in tokens
        if token.map and token.type in {"fence", "code_block"}
    ]
    # Feed only actual HTML markup to the shared literal parser. Escaped tags
    # and tags inside code examples must not open or close an HTML literal.
    markup = [char if char in "\r\n" else " " for char in text]
    for block in tokens:
        html = [child for child in block.children or [] if child.type == "html_inline"]
        if not block.map or (block.type != "html_block" and not html):
            continue
        if block.type == "html_block":
            start, end = (offsets[line] for line in block.map)
            markup[start:end] = text[start:end]
            continue
        positions: list[int] = []
        row = block.map[0]
        for line in block.content.split("\n"):
            if row + 1 >= len(offsets) and not line:
                break
            raw = text[offsets[row] : offsets[row + 1]].rstrip("\r\n").replace("\x00", "\ufffd")
            shift = raw.find(line)
            if shift < 0:
                raise ValueError("Cannot locate HTML markup in its original source line")
            positions.extend(offsets[row] + shift + i for i in range(len(line)))
            positions.append(offsets[row] + len(raw))
            row += 1
        for child in html:
            start, end = child.meta["original_markup_span"]
            left, right = positions[start], positions[end - 1] + 1
            markup[left:right] = text[left:right]
    spans.extend(_html_literal_spans("".join(markup)))
    return tuple(sorted(spans))


def _html_literal_spans(markup: str) -> list[tuple[int, int]]:
    from markdown_it.common.html_blocks import block_names

    from openkb.html_images import HTMLLiteralContext

    offsets = [0, *(match.end() for match in re.finditer("\n", markup))]
    visible = []
    void = {
        "area",
        "base",
        "basefont",
        "br",
        "col",
        "embed",
        "frame",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    class LocatedLiterals(HTMLLiteralContext):
        def __init__(self):
            super().__init__()
            # Unescape would change data lengths and invalidate native offsets.
            self.convert_charrefs = False
            self.containers: list[str] = []

        def handle_starttag(self, tag, attrs):
            super().handle_starttag(tag, attrs)
            # Native commands use angle parameters such as <vm>/<snap-name>.
            # A valid inline tag token alone does not prove a content container.
            if not self.literal and tag in block_names and tag not in void:
                self.containers.append(tag)

        def handle_endtag(self, tag):
            was_literal = bool(self.literal)
            super().handle_endtag(tag)
            if not was_literal and tag in self.containers:
                index = len(self.containers) - 1 - self.containers[::-1].index(tag)
                del self.containers[index:]

        def handle_data(self, data):
            if not self.literal and not self.containers:
                row, column = self.getpos()
                start = offsets[row - 1] + column
                visible.append((start, start + len(data)))

    parser = LocatedLiterals()
    parser.feed(markup)
    parser.close()
    # CommonMark HTML blocks may continue long after </table> or </div>.
    # Exclude actual markup and container contents, not that whole block's tail.
    spans = []
    cursor = 0
    for start, end in visible:
        if cursor < start:
            spans.append((cursor, start))
        cursor = end
    if cursor < len(markup):
        spans.append((cursor, len(markup)))
    return spans


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


def original_context(
    source: dict, raw, location: dict, *, match_offset: int | None = None, selected=None
) -> dict:
    """Report source spans and their evidenced heading path, with executable neighbors."""
    units = _units(raw, location.get("part"))
    # Parse the whole source so a literal block cannot become a heading merely
    # because a physical page ends inside it. The join never changes reader text.
    literals = _literal_spans("\n".join(body for _, body in units))
    base = 0
    headings = []
    for number, body in units:
        matches = [(m, True) for m in _HEADING.finditer(body)]
        matches.extend((m, False) for m in _UNCERTAIN_HEADING.finditer(body))
        for m, confirmed in sorted(matches, key=lambda item: item[0].start()):
            if any(start <= base + m.start() < end for start, end in literals):
                continue
            title = m[3].strip().rstrip("#").strip() if confirmed else m[0]
            if len(title) > 180 or re.search(r"\.{3,}|…{2,}", title):
                continue
            headings.append(
                {
                    "unit": number,
                    "offset": m.start(),
                    "text": title,
                    "literal": m[0],
                    "level": (len(m[1]) if m[1] else len(m[2].split("."))) if confirmed else None,
                    "number": m[2] if confirmed else None,
                }
            )
        base += len(body) + 1
    # Repeated running headers are not reliable section boundaries.
    repeated = {
        h["literal"]
        for h in headings
        if h["level"] is not None
        and len({v["unit"] for v in headings if v["literal"] == h["literal"]}) > 1
    }
    headings = [h for h in headings if h["literal"] not in repeated]
    stack: list[dict] = []
    for h in headings:
        if h["level"] is None:
            # A label such as "5.2Deployment" is lexically indistinguishable
            # from a quantity/version. It stops ancestry without certifying one.
            stack.clear()
            h["path"] = []
            continue
        # Depth alone cannot make an earlier numbered step or quantity the
        # ancestor of a different chapter. Numbered parents must share a prefix.
        while stack and (
            stack[-1]["level"] >= h["level"]
            or (
                h["number"]
                and any(
                    parent["number"] and not h["number"].startswith(parent["number"] + ".")
                    for parent in stack
                )
            )
        ):
            stack.pop()
        stack.append({k: v for k, v in h.items() if k != "path"})
        # Keep ancestry checks internal; expose literal source positions only.
        h["path"] = [
            {k: v for k, v in item.items() if k not in {"level", "number"}} for item in stack
        ]
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
        "scope_rule": "Keep each heading's object, operation and conditions together; "
        "unconfirmed ancestry is unknown.",
    }
