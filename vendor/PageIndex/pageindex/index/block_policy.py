"""Explicit content-block locations for the existing content-based tree algorithm.

Only the location/title contract differs from PDF. Grouping, recursive thresholds,
original-text summaries, document description and tree construction stay shared.
"""

import copy
import json
import re


class BlockPolicy:
    def __init__(self, metadata):
        source = metadata.get("source", {})
        self.text = source.get("text")
        self.units = source.get("blocks")
        if not isinstance(self.text, str) or not isinstance(self.units, list) or not self.units:
            raise ValueError("Block policy requires frozen text and units")
        cursor = 0
        for ordinal, unit in enumerate(self.units, 1):
            if unit.get("ordinal") != ordinal or not unit.get("source_spans"):
                raise ValueError("Invalid block ordinals or coverage")
            for start, end in unit["source_spans"]:
                if start != cursor or not start <= end <= len(self.text):
                    raise ValueError("Invalid block source spans")
                cursor = end
        if cursor != len(self.text) or metadata.get("unit_count") != len(self.units):
            raise ValueError("Incomplete block coverage")

    def render(self, ordinal):
        unit = self.units[ordinal - 1]
        body = [{"range": span, "text": self.text[span[0]:span[1]]}
                for span in unit["source_spans"]]
        return json.dumps({"unit": ordinal, "part": "body", "source": body,
                           "headings": unit.get("headings", []),
                           "display_context": unit.get("display_context", [])}, ensure_ascii=False)

    def prompt(self, part, previous=None):
        return """CONTENT BLOCK STRUCTURE
You are an expert in extracting hierarchical tree structure from original content.
Generate a complete hierarchical table of contents for these content blocks.
These are NOT physical pages. Navigation lists/links are only hints; locate sections
in their actual original bodies. No printed-page numbers or page-offset guessing.
Use an original heading when present (title_origin="original"). For unheaded text,
generate a concise title (title_origin="generated"); it need not appear verbatim.
Every title MUST have a real anchor in the original body: unit ordinal, part="body",
range=[start,end] in zero-based Unicode code points (end exclusive), exact excerpt.
Display context (repeated headings/headers/fences) is supplementary, never an anchor.
Return a JSON list of {structure: "1.1", title, title_origin, physical_index: integer,
anchor: {unit: integer, part: "body", range: [start,end], excerpt: "exact original"}}.
physical_index is the block ordinal and must match anchor.unit. Include each section
once. A single root is valid for an unheaded document even when it spans many blocks.
Continue any previous structure with additional sections only. No other output.
""" + "\nPrevious structure:\n" + json.dumps(previous or [], ensure_ascii=False) + "\nOriginal blocks:\n" + part

    def valid(self, item, start_index=1, count=None):
        anchor = item.get("anchor")
        if not isinstance(anchor, dict) or item.get("title_origin") not in {"original", "generated"}:
            return False
        if not isinstance(item.get("structure"), str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", item["structure"]):
            return False
        number = item.get("physical_index")
        stop = start_index + (len(self.units) if count is None else count)
        if type(number) is not int or not start_index <= number < stop or not 1 <= number <= len(self.units):
            return False
        span = anchor.get("range")
        if (type(anchor.get("unit")) is not int or anchor.get("unit") != number or anchor.get("part") != "body"
                or not isinstance(span, list) or len(span) != 2
                or any(type(value) is not int for value in span)):
            return False
        left, right = span
        unit = self.units[number - 1]
        if (not left < right or not any(a <= left < right <= b for a, b in unit["source_spans"])
                or self.text[left:right] != anchor.get("excerpt")
                or not isinstance(item.get("title"), str) or not item["title"].strip()):
            return False
        if item["title_origin"] == "original":
            return any(heading["title"] == item["title"]
                       and heading["source_span"][0] <= left < right <= heading["source_span"][1]
                       for heading in unit.get("headings", []))
        return True

    def validate_order(self, items, start_index=1, count=None):
        if not all(self.valid(item, start_index, count) for item in items):
            raise ValueError("Unverified content-block anchors after correction")
        positions = [(item["physical_index"], item["anchor"]["range"][0]) for item in items]
        if positions != sorted(positions) or len({item["structure"] for item in items}) != len(items):
            raise ValueError("Block sections must have distinct structure IDs in original order")

    def starts_unit(self, item):
        if not self.valid(item):
            return "no"
        unit = self.units[item["physical_index"] - 1]
        left = unit["source_spans"][0][0]
        return "yes" if not self.text[left:item["anchor"]["range"][0]].strip() else "no"

    def preface(self, items):
        if items and items[0]["physical_index"] > 1:
            left, right = self.units[0]["source_spans"][0]
            if left == right:
                raise ValueError("Cannot anchor an empty preface block")
            end = min(right, left + 80)
            items.insert(0, {"structure": "0", "title": "Preface", "title_origin": "generated",
                             "physical_index": 1, "anchor": {"unit": 1, "part": "body",
                             "range": [left, end], "excerpt": self.text[left:end]}})
        for item in items:
            item["unit_kind"] = "block"
        return items

    async def fix(self, item, first, last, model, complete, extract):
        prompt = self.prompt("\n".join(self.render(i) for i in range(first, last + 1)))
        prompt += "\nCorrect ONLY this entry's original anchor/title; return one list entry:\n" + json.dumps(item)
        result = extract(await complete(model=model, prompt=prompt))
        if isinstance(result, list) and len(result) == 1 and self.valid(result[0], first, last-first+1):
            candidate = result[0]
            # Internal list_index/is_valid and unrelated fields are never model
            # output. The caller alone decides which section is being repaired.
            return {key: copy.deepcopy(candidate[key])
                    for key in ("title", "title_origin", "physical_index", "anchor")}
        return None
