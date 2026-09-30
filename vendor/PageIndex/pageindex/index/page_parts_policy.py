"""Physical PDF coordinates with separately attributable original speaker notes."""

import copy
import json
import re


class PagePartsPolicy:
    unit_kind = "page"

    def __init__(self, metadata):
        self.parts = metadata.get("page_parts")
        if (not isinstance(self.parts, list) or not self.parts
                or len(self.parts) != metadata.get("unit_count")
                or any(not isinstance(unit, dict) or set(unit) != {"body", "notes"}
                       or any(not isinstance(text, str) for text in unit.values())
                       for unit in self.parts)):
            raise ValueError("Physical slide policy requires complete body/notes parts")

    def render(self, ordinal):
        return json.dumps({"unit": ordinal, "parts": self.parts[ordinal - 1]}, ensure_ascii=False)

    def prompt(self, part, previous=None):
        return """PHYSICAL SLIDE STRUCTURE
You are an expert in extracting hierarchical tree structure from original content.
Generate a hierarchical table of contents for these physical slides. Each has
separate body and speaker notes. Notes belong to the SAME physical page; never
create additional pages. Use either part for navigation while retaining its domain.
Navigation titles are generated, not original source headings: title_origin="generated".
Every title needs an exact original anchor: unit ordinal, part="body" or "notes",
range=[start,end] in zero-based Unicode code points within THAT part, and exact excerpt.
Do not anchor on generated page or notes labels. Return a JSON list of
{structure:"1.1", title, title_origin:"generated", physical_index:integer,
anchor:{unit:integer, part:"body"|"notes", range:[start,end], excerpt:"exact original"}}.
physical_index must equal anchor.unit. Keep original order, body before notes on
each slide. A single root covering the whole presentation is valid. No other output.
""" + "\nPrevious structure:\n" + json.dumps(previous or [], ensure_ascii=False) + "\nSlides:\n" + part

    def valid(self, item, start_index=1, count=None):
        if (item.get("title_origin") != "generated" or not isinstance(item.get("title"), str)
                or not item["title"].strip() or not isinstance(item.get("structure"), str)
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", item["structure"])):
            return False
        number, anchor = item.get("physical_index"), item.get("anchor")
        stop = start_index + (len(self.parts) if count is None else count)
        if (type(number) is not int or not start_index <= number < stop
                or not 1 <= number <= len(self.parts) or not isinstance(anchor, dict)
                or type(anchor.get("unit")) is not int or anchor["unit"] != number
                or anchor.get("part") not in {"body", "notes"}):
            return False
        span = anchor.get("range")
        if (not isinstance(span, list) or len(span) != 2
                or any(type(value) is not int for value in span)):
            return False
        left, right = span
        text = self.parts[number - 1][anchor["part"]]
        return 0 <= left < right <= len(text) and text[left:right] == anchor.get("excerpt")

    def validate_order(self, items, start_index=1, count=None):
        if not all(self.valid(item, start_index, count) for item in items):
            raise ValueError("Unverified physical slide anchors after correction")
        positions = [(item["physical_index"], item["anchor"]["part"] == "notes",
                      item["anchor"]["range"][0]) for item in items]
        if positions != sorted(positions) or len({item["structure"] for item in items}) != len(items):
            raise ValueError("Slide sections must have distinct IDs in original order")

    def starts_unit(self, item):
        if not self.valid(item):
            return "no"
        parts = self.parts[item["physical_index"] - 1]
        anchor = item["anchor"]
        prior = parts[anchor["part"]][:anchor["range"][0]]
        if anchor["part"] == "notes":
            prior = parts["body"] + prior
        return "no" if prior.strip() else "yes"

    def preface(self, items):
        if items and items[0]["physical_index"] > 1:
            for number, parts in enumerate(self.parts[:items[0]["physical_index"] - 1], 1):
                candidates = [(part, parts[part]) for part in ("body", "notes") if parts[part].strip()]
                if candidates:
                    part, text = candidates[0]
                    end = min(len(text), 80)
                    items.insert(0, {"structure": "0", "title": "Preface", "title_origin": "generated",
                                     "physical_index": number, "anchor": {"unit": number, "part": part,
                                     "range": [0, end], "excerpt": text[:end]}})
                    break
        for item in items:
            item["unit_kind"] = self.unit_kind
        self.validate_order(items)
        return items

    async def fix(self, item, first, last, model, complete, extract):
        prompt = self.prompt("\n".join(self.render(i) for i in range(first, last + 1)))
        prompt += "\nCorrect ONLY this entry's anchor/title; return one list entry:\n" + json.dumps(item)
        result = extract(await complete(model=model, prompt=prompt))
        if isinstance(result, list) and len(result) == 1 and self.valid(result[0], first, last-first+1):
            return {key: copy.deepcopy(result[0][key])
                    for key in ("title", "title_origin", "physical_index", "anchor")}
        return None
