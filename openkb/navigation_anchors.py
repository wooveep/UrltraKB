"""Reconcile navigation anchors without changing immutable parser transcriptions."""

import math
import re


class AnchorLookup:
    """Read each relevant physical page once, including paragraph transcriptions."""

    def __init__(self, source, parsed, reader):
        self.source, self.parsed, self.reader = source, parsed, reader
        self.pages, self.titles = {}, {}
        for block in parsed.blocks:
            page = block.location.get("page")
            if (
                block.location.get("kind") == "pdf"
                and type(page) is int
                and block.kind not in {"metadata", "image"}
                and block.location.get("role") not in {"toc", "header", "footer"}
            ):
                self.pages.setdefault(page, []).append(block)

    def merge(self, sections, candidate):
        from openkb.evidence import Evidence, complete_read_bound
        from openkb.navigation_structure import native_title

        candidate = self.complete_numbered_title(candidate)
        merge_section(sections, candidate, self.parsed)
        if candidate["title_origin"] != "source":
            return
        page = self.parsed.blocks[candidate["order"]].location.get("page")
        if page not in self.pages:
            return
        if page not in self.titles:
            titles = {}
            for block in self.pages[page]:
                view = self.reader.read(
                    Evidence(self.source.source_id, self.source.id, self.parsed.id, block.id),
                    max_chars=complete_read_bound(block),
                )
                title = native_title({"text": view.text})
                if not 0 < len(title) <= 320:
                    continue
                row = section_candidate(
                    {
                        "title": title,
                        "title_origin": "source",
                        "level": None,
                        "start_block": block.id,
                        "anchor": title,
                        "order": block.order,
                    },
                    self.parsed,
                    "unknown",
                )
                titles.setdefault("".join(title.split()), []).append(row)
            self.titles[page] = titles
        for row in self.titles[page].get("".join(candidate["title"].split()), []):
            if _same_position(candidate, row, self.parsed):
                merge_section(sections, row, self.parsed)

    def complete_numbered_title(self, candidate):
        """A number on its own line is part of the same short PDF title block."""
        from openkb.evidence import Evidence, complete_read_bound

        block = self.parsed.blocks[candidate["order"]]
        if (
            candidate["title_origin"] != "source"
            or block.location.get("kind") != "pdf"
            or not re.fullmatch(r"\d+(?:\.\d+)*[.)、]?", candidate["title"].strip())
        ):
            return candidate
        view = self.reader.read(
            Evidence(self.source.source_id, self.source.id, self.parsed.id, block.id),
            max_chars=complete_read_bound(block),
        )
        lines = [line.strip() for line in view.text.splitlines() if line.strip()]
        if len(lines) != 2 or lines[0] != candidate["title"].strip() or len(" ".join(lines)) > 320:
            return candidate
        expanded = section_candidate(
            {**candidate, "title": " ".join(lines)}, self.parsed, candidate["level_origin"]
        )
        expanded["anchors"] = candidate["anchors"] + expanded["anchors"]
        return expanded


def section_candidate(row, parsed, origin):
    """Location and hierarchy have separate provenance; missing depth stays unknown."""
    block = parsed.blocks[row["order"]]
    level, level_origin = row.get("level"), origin
    native = block.location.get("heading_level")
    if type(native) is int and 1 <= native <= 9:
        level, level_origin = native, "native"
    elif row["title_origin"] == "source":
        numbered = re.match(r"^([1-9]\d*(?:\.\d+)*)(?:[.)、]\s*|\s+)(?=[^\d\s])", row["title"])
        if numbered:
            level, level_origin = min(9, numbered[1].count(".") + 1), "numbering"
    if level is None:
        level_origin = "unknown"
    anchor = {
        "block_id": row["start_block"],
        "title": row["title"],
        "level": level,
        "level_origin": level_origin,
    }
    return {**row, "level": level, "level_origin": level_origin, "anchors": [anchor]}


def _same_position(left, right, parsed):
    if left["start_block"] == right["start_block"]:
        return "".join(left["title"].split()) == "".join(right["title"].split())
    if "".join(left["title"].split()) != "".join(right["title"].split()):
        return False
    a, b = (parsed.blocks[row["order"]].location for row in (left, right))
    if a.get("kind") != "pdf" or b.get("kind") != "pdf" or a.get("page") != b.get("page"):
        return False
    if a.get("attachment") != b.get("attachment"):
        return False
    boxes = [a.get("bbox"), b.get("bbox")]
    for box in boxes:
        if (
            not isinstance(box, (list, tuple))
            or len(box) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
            or box[2] <= box[0]
            or box[3] <= box[1]
        ):
            return False
    x, y = boxes
    overlap = max(0, min(x[2], y[2]) - max(x[0], y[0])) * max(0, min(x[3], y[3]) - max(x[1], y[1]))
    areas = [(box[2] - box[0]) * (box[3] - box[1]) for box in boxes]
    return overlap >= 0.8 * min(areas) and min(areas) >= 0.5 * max(areas)


def merge_section(sections, candidate, parsed):
    """One physical source heading has one boundary, retaining all observed anchors."""
    rank = {"unknown": 0, "toc": 1, "model": 2, "numbering": 3, "native": 4}
    for i, old in enumerate(sections):
        if old == candidate:
            return
        same_anchor = any(
            a["block_id"] == b["block_id"]
            and "".join(a["title"].split()) == "".join(b["title"].split())
            for a in old["anchors"]
            for b in candidate["anchors"]
        )
        if not same_anchor and (
            old["title_origin"] != "source"
            or candidate["title_origin"] != "source"
            or not _same_position(old, candidate, parsed)
        ):
            continue
        anchors = old["anchors"] + [a for a in candidate["anchors"] if a not in old["anchors"]]
        # Prefer the earliest original block as the boundary, not the later OCR copy.
        first = min(
            (old, candidate), key=lambda row: (row["order"], row["title_origin"] != "source")
        )
        strongest = max((old, candidate), key=lambda row: rank[row["level_origin"]])
        sections[i] = {
            **first,
            "level": strongest["level"],
            "level_origin": strongest["level_origin"],
            "anchors": anchors,
        }
        return
    sections.append(candidate)


def inherit_summaries(nodes, previous):
    """Only an unchanged original range/title may reuse a prior summary outcome."""
    by_range = {(n["start"], n["end"], n["title"]): n for n in previous}
    for node in reversed(nodes):
        old = by_range.get((node["start"], node["end"], node["title"]))
        if old is not None:
            details = old.get("summary_details", {})
            if details.get("basis") in {"derived", "mixed"}:
                from openkb.navigation_summary_inputs import child_signature

                children = [n for n in nodes if n["parent"] == node["id"]]
                if details.get("input_signature") != child_signature(children):
                    continue
            for key in ("summary", "summary_origin", "summary_details"):
                if key in old:
                    node[key] = old[key]
