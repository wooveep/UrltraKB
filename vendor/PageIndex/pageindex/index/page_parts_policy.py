"""Physical PDF coordinates with separately attributable original speaker notes."""

import copy
import json
import re

PAGE_PARTS_INDEX_POLICY = "physical-slide-navigation-v3"


class PagePartsContractError(ValueError):
    """Frozen slide content cannot support the requested text navigation."""


class PagePartsPolicy:
    unit_kind = "page"

    def __init__(self, metadata):
        self.parts = metadata.get("page_parts")
        if (
            not isinstance(self.parts, list)
            or not self.parts
            or len(self.parts) != metadata.get("unit_count")
            or any(
                not isinstance(unit, dict)
                or set(unit) != {"body", "notes"}
                or any(not isinstance(text, str) for text in unit.values())
                for unit in self.parts
            )
        ):
            raise ValueError("Physical slide policy requires complete body/notes parts")
        self.generated = metadata.get("page_generated_spans")
        if self.generated is not None:
            if not isinstance(self.generated, list) or len(self.generated) != len(self.parts):
                raise ValueError("Physical slide generated spans lack complete coverage")
            for parts, spans in zip(self.parts, self.generated):
                stop = 0
                if not isinstance(spans, list):
                    raise ValueError("Invalid physical slide generated spans")
                for span in spans:
                    if (
                        not isinstance(span, list)
                        or len(span) != 2
                        or any(type(n) is not int for n in span)
                        or not stop <= span[0] < span[1] <= len(parts["body"])
                    ):
                        raise ValueError("Invalid physical slide generated span")
                    stop = span[1]

    def original_ranges(self, number, part, stop=None):
        text = self.parts[number - 1][part]
        stop = len(text) if stop is None else stop
        cursor = 0
        spans = self.generated[number - 1] if part == "body" and self.generated is not None else []
        for left, right in spans:
            if cursor < min(left, stop):
                yield cursor, min(left, stop)
            cursor = right
        if cursor < stop:
            yield cursor, stop

    def original_text(self, number, part, stop=None):
        text = self.parts[number - 1][part]
        return "".join(text[a:b] for a, b in self.original_ranges(number, part, stop))

    def original_anchor(self, number, part):
        text = self.parts[number - 1][part]
        for left, right in self.original_ranges(number, part):
            if text[left:right].strip():
                left += len(text[left:right]) - len(text[left:right].lstrip())
                right = min(right, left + 80)
                return {
                    "unit": number,
                    "part": part,
                    "range": [left, right],
                    "excerpt": text[left:right],
                }
        return None

    def has_navigation_text(self, first=1, last=None):
        last = len(self.parts) if last is None else last
        if (
            type(first) is not int
            or type(last) is not int
            or not 1 <= first <= last <= len(self.parts)
        ):
            raise PagePartsContractError("Invalid physical slide navigation range")
        return any(
            self.original_text(number, part).strip()
            for number in range(first, last + 1)
            for part in ("body", "notes")
        )

    def require_navigation_text(self, first=1, last=None):
        if not self.has_navigation_text(first, last):
            raise PagePartsContractError(
                "No content is available for text navigation (body/notes are empty)"
            )

    def _textless_entry(self, item, first, last):
        number, anchor = item.get("physical_index"), item.get("anchor")
        if (
            type(number) is not int
            or not first <= number <= last
            or not 1 <= number <= len(self.parts)
            or not isinstance(anchor, dict)
            or type(anchor.get("unit")) is not int
            or anchor["unit"] != number
            or anchor.get("part") not in {"body", "notes"}
            or item.get("title_origin") != "generated"
            or not isinstance(item.get("title"), str)
            or not item["title"].strip()
            or not isinstance(item.get("structure"), str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", item["structure"])
        ):
            return False
        parts = self.parts[number - 1]
        if any(self.original_text(number, part).strip() for part in parts):
            return False
        span = anchor.get("range")
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(value) is not int for value in span)
        ):
            return False
        left, right = span
        text = parts[anchor["part"]]
        return 0 <= left <= right <= len(text) and text[left:right] == anchor.get("excerpt")

    def prepare_navigation(self, items, first, last):
        self.require_navigation_text(first, last)
        structures = [item.get("structure") for item in items]
        if any(structures.count(value) > 1 for value in structures):
            raise ValueError("Slide sections must have distinct IDs in original order")
        # The existing flat-tree builder promotes surviving children when their
        # removed parent is absent. Neither original pages nor anchors are edited.
        retained = [item for item in items if not self._textless_entry(item, first, last)]
        if retained:
            return retained
        for number in range(first, last + 1):
            for part in self.parts[number - 1]:
                anchor = self.original_anchor(number, part)
                if anchor:
                    return [
                        {
                            "structure": "0",
                            "title": "Presentation",
                            "title_origin": "generated",
                            "physical_index": number,
                            "anchor": anchor,
                        }
                    ]

    def cover_range(self, items, first, last):
        if items:
            items[0]["start_index"] = first
        return items

    def render(self, ordinal):
        return json.dumps(
            {
                "unit": ordinal,
                "parts": self.parts[ordinal - 1],
                **(
                    {"body_generated_spans": self.generated[ordinal - 1]}
                    if self.generated is not None
                    else {}
                ),
            },
            ensure_ascii=False,
        )

    def prompt(self, part, previous=None):
        return (
            """PHYSICAL SLIDE STRUCTURE
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
When BOTH body and notes have no non-whitespace text, do not generate an independent
text navigation entry. The physical slide and its images are still retained.
body_generated_spans, when recorded, identify generated media syntax, not original
words. Do not anchor on them or use them as evidence of nonempty original text.
"""
            + (
                "\nReturn ONLY new entries for this chunk. Previous structure is immutable context: "
                "never repeat, revise or renumber its entries. Continue unique structure IDs. "
                "Return [] if no additional section begins here.\n"
                if previous is not None
                else ""
            )
            + "\nPrevious structure:\n"
            + json.dumps(previous or [], ensure_ascii=False)
            + "\nSlides:\n"
            + part
        )

    def validate_chunk(self, previous, incoming, part):
        """Validate the incremental boundary before mutating the accumulated tree."""
        if not isinstance(incoming, list) or any(not isinstance(item, dict) for item in incoming):
            raise PagePartsContractError("slide_toc_chunk: response must be an array of entries")
        old_ids = {item.get("structure") for item in previous}
        ids = [item.get("structure") for item in incoming]
        if any(not isinstance(value, str) for value in ids):
            raise PagePartsContractError("slide_toc_chunk: missing string structure ID")
        conflicts = old_ids.intersection(ids)
        if conflicts or len(set(ids)) != len(ids):
            raise PagePartsContractError(
                f"slide_toc_chunk: duplicate/conflicting IDs {sorted(conflicts or set(ids))}"
            )
        ordinals = {int(number) for number in re.findall(r"<physical_index_(\d+)>", part)}
        if any(item.get("physical_index") not in ordinals for item in incoming):
            raise PagePartsContractError("slide_toc_chunk: anchor is outside the current chunk")
        try:
            # Textless placeholders are removed later, but still require valid coordinates.
            retained = [
                item for item in incoming if not self._textless_entry(item, 1, len(self.parts))
            ]
            self.validate_order(retained)
            # Earlier anchors have their own verification/correction phase. Do
            # not ask a continuation to change immutable previous entries.
            ordered = [
                item
                for item in previous + incoming
                if not self._textless_entry(item, 1, len(self.parts))
            ]
            positions = [
                (
                    item["physical_index"],
                    item["anchor"]["part"] == "notes",
                    item["anchor"]["range"][0],
                )
                for item in ordered
            ]
            if positions != sorted(positions):
                raise ValueError("New entries must follow the previous original positions")
        except (ValueError, KeyError, TypeError) as error:
            raise PagePartsContractError(f"slide_toc_chunk: {error}") from error

    def valid(self, item, start_index=1, count=None):
        if (
            item.get("title_origin") != "generated"
            or not isinstance(item.get("title"), str)
            or not item["title"].strip()
            or not isinstance(item.get("structure"), str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", item["structure"])
        ):
            return False
        number, anchor = item.get("physical_index"), item.get("anchor")
        stop = start_index + (len(self.parts) if count is None else count)
        if (
            type(number) is not int
            or not start_index <= number < stop
            or not 1 <= number <= len(self.parts)
            or not isinstance(anchor, dict)
            or type(anchor.get("unit")) is not int
            or anchor["unit"] != number
            or anchor.get("part") not in {"body", "notes"}
        ):
            return False
        span = anchor.get("range")
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(value) is not int for value in span)
        ):
            return False
        left, right = span
        text = self.parts[number - 1][anchor["part"]]
        return (
            0 <= left < right <= len(text)
            and text[left:right] == anchor.get("excerpt")
            and any(a <= left < right <= b for a, b in self.original_ranges(number, anchor["part"]))
        )

    def validate_order(self, items, start_index=1, count=None):
        if not all(self.valid(item, start_index, count) for item in items):
            raise ValueError("Unverified physical slide anchors after correction")
        positions = [
            (item["physical_index"], item["anchor"]["part"] == "notes", item["anchor"]["range"][0])
            for item in items
        ]
        if positions != sorted(positions) or len({item["structure"] for item in items}) != len(
            items
        ):
            raise ValueError("Slide sections must have distinct IDs in original order")

    def starts_unit(self, item):
        if not self.valid(item):
            return "no"
        parts = self.parts[item["physical_index"] - 1]
        anchor = item["anchor"]
        prior = self.original_text(item["physical_index"], anchor["part"], anchor["range"][0])
        if anchor["part"] == "notes":
            prior = self.original_text(item["physical_index"], "body") + prior
        return "no" if prior.strip() else "yes"

    def preface(self, items):
        if items and items[0]["physical_index"] > 1:
            for number, parts in enumerate(self.parts[: items[0]["physical_index"] - 1], 1):
                candidates = [
                    (part, self.original_anchor(number, part))
                    for part in ("body", "notes")
                    if self.original_text(number, part).strip()
                ]
                if candidates:
                    part, anchor = candidates[0]
                    if self.generated is None:
                        text = parts[part]
                        anchor = {
                            "unit": number,
                            "part": part,
                            "range": [0, min(len(text), 80)],
                            "excerpt": text[:80],
                        }
                    items.insert(
                        0,
                        {
                            "structure": "0",
                            "title": "Preface",
                            "title_origin": "generated",
                            "physical_index": number,
                            "anchor": anchor,
                        },
                    )
                    break
        for item in items:
            item["unit_kind"] = self.unit_kind
        self.validate_order(items)
        return items

    async def fix(self, item, first, last, model, complete, extract):
        if self._textless_entry(item, first, last):
            raise PagePartsContractError("Textless slide entry requires navigation preprocessing")
        prompt = self.prompt("\n".join(self.render(i) for i in range(first, last + 1)))
        prompt += "\nCorrect ONLY this entry's anchor/title; return one list entry:\n" + json.dumps(
            item
        )
        result = extract(await complete(model=model, prompt=prompt))
        if (
            isinstance(result, list)
            and len(result) == 1
            and self.valid(result[0], first, last - first + 1)
        ):
            return {
                key: copy.deepcopy(result[0][key])
                for key in ("title", "title_origin", "physical_index", "anchor")
            }
        return None
