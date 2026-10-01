"""Explicit content-block locations for the existing content-based tree algorithm.

Only the location/title contract differs from PDF. Grouping, recursive thresholds,
original-text summaries, document description and tree construction stay shared.
"""

import copy
import json
import re

BLOCK_INDEX_POLICY = "original-anchor-v2:normalize-title-origin:bounded-correction"


class BlockContractError(ValueError):
    """Deterministic block contract failure; retrying the whole index is unsafe."""


class BlockPolicy:
    unit_kind = "block"

    def __init__(self, metadata):
        source = metadata.get("source", {})
        self.text = source.get("text")
        self.units = source.get("blocks")
        self.generated = [origin["normalized_span"] for origin in source.get("origins", [])
                          if origin.get("kind") in {"generated", "csv_separator"}]
        self.evidence = [origin["normalized_span"] for origin in source.get("origins", [])
                         if origin.get("kind") not in {"generated", "csv_separator"}]
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
                           "generated_formatting": [span for span in self.generated
                                                    if any(a < span[1] and b > span[0]
                                                           for a, b in unit["source_spans"])],
                           "headings": unit.get("headings", []),
                           "original_evidence": [
                               {"range": [max(a, left), min(b, right)],
                                "excerpt": self.text[max(a, left):min(b, right)]}
                               for a, b in self.evidence
                               for left, right in unit["source_spans"]
                               if a < right and b > left],
                           "display_context": unit.get("display_context", [])}, ensure_ascii=False)

    def prompt(self, part, previous=None):
        return """CONTENT BLOCK STRUCTURE
You are an expert in extracting hierarchical tree structure from original content.
Generate a complete hierarchical table of contents for these content blocks.
These are NOT physical pages. Navigation lists/links are only hints; locate sections
in their actual original bodies. No printed-page numbers or page-offset guessing.
Use an original heading when present (title_origin="original"). For unheaded text,
generate a concise title (title_origin="generated"); it need not appear verbatim.
The ONLY allowed original titles are the headings declared in each block's
headings list, anchored inside that heading's source_span. If headings is empty,
title_origin MUST be "generated". A sheet name or display context is not a heading.
Every title MUST have a real anchor in the original body: unit ordinal, part="body",
range=[start,end] in zero-based Unicode code points (end exclusive), exact excerpt.
Display context (repeated headings/headers/fences) is supplementary, never an anchor.
Generated formatting ranges (such as synthetic CSV column names) are not original
evidence. Anchor only outside these ranges, in an actual source value or text.
When original_evidence is listed, copy one of its range/excerpt pairs exactly;
these are precomputed original evidence spans. Do not guess character offsets or
use the whole block range when it also includes generated formatting.
Return a JSON list of {structure: "1.1", title, title_origin, physical_index: integer,
anchor: {unit: integer, part: "body", range: [start,end], excerpt: "exact original"}}.
physical_index is the block ordinal and must match anchor.unit. Include each section
once. A single root is valid for an unheaded document even when it spans many blocks.
Continue any previous structure with additional sections only. No other output.
""" + "\nPrevious structure:\n" + json.dumps(previous or [], ensure_ascii=False) + "\nOriginal blocks:\n" + part

    def validate_entry(self, item, start_index=1, count=None):
        """Return a stable rejection reason without weakening original anchors."""
        if not isinstance(item, dict):
            return "entry_shape"
        anchor = item.get("anchor")
        if not isinstance(anchor, dict) or item.get("title_origin") not in {"original", "generated"}:
            return "anchor_or_title_origin"
        if not isinstance(item.get("structure"), str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", item["structure"]):
            return "structure"
        number = item.get("physical_index")
        stop = start_index + (len(self.units) if count is None else count)
        if type(number) is not int or not start_index <= number < stop or not 1 <= number <= len(self.units):
            return "physical_index"
        span = anchor.get("range")
        if (type(anchor.get("unit")) is not int or anchor.get("unit") != number or anchor.get("part") != "body"
                or not isinstance(span, list) or len(span) != 2
                or any(type(value) is not int for value in span)):
            return "anchor_shape"
        left, right = span
        unit = self.units[number - 1]
        if not left < right or not any(a <= left < right <= b for a, b in unit["source_spans"]):
            return "anchor_range"
        if any(left < b and right > a for a, b in self.generated):
            return "generated_evidence"
        if self.text[left:right] != anchor.get("excerpt"):
            return "excerpt_mismatch"
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            return "title"
        if item["title_origin"] == "original":
            headings = unit.get("headings", [])
            if not headings:
                return "missing_original_heading"
            if not any(heading["title"] == item["title"] for heading in headings):
                return "undeclared_original_title"
            if not any(heading["title"] == item["title"]
                       and heading["source_span"][0] <= left < right <= heading["source_span"][1]
                       for heading in headings):
                return "original_heading_anchor_mismatch"
        return None

    def valid(self, item, start_index=1, count=None):
        return self.validate_entry(item, start_index, count) is None

    def normalize_title_origin(self, item, start_index=1, count=None):
        """Only relabel a fully verified original anchor lacking a declared heading."""
        if self.validate_entry(item, start_index, count) != "missing_original_heading":
            return False
        candidate = dict(item, title_origin="generated")
        if not self.valid(candidate, start_index, count):
            return False
        item["title_origin"] = "generated"
        return True

    def validate_order(self, items, start_index=1, count=None):
        if not all(self.valid(item, start_index, count) for item in items):
            raise BlockContractError("Unverified content-block anchors after correction")
        positions = [(item["physical_index"], item["anchor"]["range"][0]) for item in items]
        if positions != sorted(positions) or len({item["structure"] for item in items}) != len(items):
            raise BlockContractError("Block sections must have distinct structure IDs in original order")

    def starts_unit(self, item):
        if not self.valid(item):
            return "no"
        unit = self.units[item["physical_index"] - 1]
        left = unit["source_spans"][0][0]
        return "yes" if not self.text[left:item["anchor"]["range"][0]].strip() else "no"

    def preface(self, items):
        if items and items[0]["physical_index"] > 1:
            # Wide generated table headers can fill a block. Skip their ranges
            # when finding evidence for an otherwise omitted source preface.
            for unit in self.units[:items[0]["physical_index"] - 1]:
                spans = unit["source_spans"]
                evidence = []
                for start, end in spans:
                    cursor = start
                    for a, b in self.generated:
                        if b <= cursor or a >= end:
                            continue
                        if cursor < a:
                            evidence.append((cursor, min(a, end)))
                        cursor = max(cursor, b)
                    if cursor < end:
                        evidence.append((cursor, end))
                evidence = [(a, b) for a, b in evidence if self.text[a:b].strip()]
                if evidence:
                    left, right = evidence[0]
                    end = min(right, left + 80)
                    number = unit["ordinal"]
                    items.insert(0, {"structure": "0", "title": "Preface", "title_origin": "generated",
                                     "physical_index": number, "anchor": {"unit": number, "part": "body",
                                     "range": [left, end], "excerpt": self.text[left:end]}})
                    break
        for item in items:
            item["unit_kind"] = "block"
        return items

    async def fix(self, item, first, last, model, complete, extract):
        if self.normalize_title_origin(item, first, last-first+1):
            return {key: copy.deepcopy(item[key])
                    for key in ("title", "title_origin", "physical_index", "anchor")}
        prompt = self.prompt("\n".join(self.render(i) for i in range(first, last + 1)))
        prompt += "\nValidation failure: " + str(self.validate_entry(item, first, last-first+1))
        prompt += "\nCorrect ONLY this entry's original anchor/title; return one list entry:\n" + json.dumps(item)
        result = extract(await complete(model=model, prompt=prompt))
        if isinstance(result, list) and len(result) == 1:
            self.normalize_title_origin(result[0], first, last-first+1)
        if isinstance(result, list) and len(result) == 1 and self.valid(result[0], first, last-first+1):
            candidate = result[0]
            # Internal list_index/is_valid and unrelated fields are never model
            # output. The caller alone decides which section is being repaired.
            return {key: copy.deepcopy(candidate[key])
                    for key in ("title", "title_origin", "physical_index", "anchor")}
        return None
