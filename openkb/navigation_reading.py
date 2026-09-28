"""Reading boundaries over saved positions; never synthesize physical page numbers."""

import re
from bisect import bisect_left, bisect_right

DEFAULT_WINDOW_TOKENS = 20000


def _unit(block):
    loc = block.location
    attachment = repr(loc.get("attachment"))
    for key in ("page", "slide"):
        if key in loc:
            return attachment, key, loc[key]
    if "sheet" in loc:
        cell = re.fullmatch(r"\$?[A-Za-z]+\$?(\d+)", loc.get("cell_address", ""))
        return attachment, "sheet", loc["sheet"], cell[1] if cell else block.order
    if "table" in loc and "row" in loc:
        return attachment, "table", loc["table"], loc["row"]
    return attachment, "block", block.order


class ReadingBoundaries:
    """Prefer whole pages/slides/rows and keep a heading with its following block."""

    def __init__(self, parsed):
        blocks = parsed.blocks
        self.ends = [
            i
            for i in range(1, len(blocks))
            if _unit(blocks[i - 1]) != _unit(blocks[i])
            and (
                _unit(blocks[i - 1])[1] != "block"
                or blocks[i - 1].kind != "heading"
                or blocks[i].kind == "heading"
            )
        ] + [len(blocks)]
        self.starts = [0, *self.ends[:-1]]

    def overlap(self, start):
        # A split oversized unit overlaps its last block, not its entire prefix.
        index = bisect_left(self.starts, start)
        if index < len(self.starts) and self.starts[index] == start:
            return self.starts[max(0, index - 1)]
        return max(0, start - 1)

    def aligned_end(self, start, end):
        index = bisect_right(self.ends, end) - 1
        return self.ends[index] if index >= 0 and self.ends[index] > start else end


_NUMBERED_TITLE = re.compile(
    r"^(?:第[零一二三四五六七八九十百千〇两\d]+[章节篇部卷]"
    r"|(?:chapter|part)\s+(?:\d+|[ivxlcdm]+)\b"
    r"|\d+(?:\.\d+)*(?:[.)、]\s*|\s+))\s*\S",
    re.IGNORECASE,
)


def possible_starts(evidence, start, end):
    """Cheap review cues, never proof of a heading or a reason to reject content."""
    cues = []
    for block in evidence["blocks"]:
        text = " ".join(block["text"].split())
        if (
            start <= block["order"] < end
            and block["kind"] == "paragraph"
            and block["location"].get("role") not in {"toc", "header", "footer"}
            and 0 < len(text) <= 160
            and _NUMBERED_TITLE.match(text)
        ):
            cues.append({"start_block": block["id"], "title": text})
    return cues


def reconsider_empty(value, cues):
    if value["sections"] or not cues:
        return None
    return {
        "structure_check": {
            "possible_starts": cues[:32],
            "instruction": (
                "Re-read the target for section starts, including paragraph blocks. These are"
                " unverified cues, not mandatory headings. Lists and ordinary prose may remain"
                " unstructured. Return only supported starts; an empty result is still allowed."
            ),
        }
    }
