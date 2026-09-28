"""One-page source-location task: tolerant hints, no page-selection authority."""

import json
import re
from dataclasses import dataclass, field

from json_repair import loads as repair_loads

from openkb.agent.document_global_context import empty_evidence, fits
from openkb.agent.document_planning_locations import (
    _SECTION_KEY,
    _literal_nodes,
    context_choices,
    resolve_hint,
)
from openkb.agent.document_planning_markdown import _FIELD, _HEADING, _ITEM, _markdown_rows
from openkb.agent.document_planning_response import _hints, _unfence
from openkb.agent.document_planning_semantics import _field_name
from openkb.agent.document_protocol import plan_messages

RULES = """Locate original sections for this ONE already selected page and its stated purpose.
Use the supplied PageIndex as navigation, not as original evidence. Return a small
Markdown list of relevant existing section keys/titles, or a Subject field. A key
with its displayed title is welcome. If nothing supports the purpose, return None.
A name appearing only in credits, a contents list or references is not substantive
support. Do not force a match. Do not change the page title/type/purpose, select other
pages, write page bodies, or supply block numbers/character offsets. Unread external
references and attachments are not supplied evidence. Locations will be read by the
application before use. If only coarse navigation is shown, a parent section may
be returned to request its existing children. Do not invent sections or JSON."""


@dataclass
class Selection:
    ranges: list = field(default_factory=list)
    clues: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    empty: bool = False
    entries: list = field(default_factory=list)


def _safe_clues(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [clue for item in value for clue in _safe_clues(item)]
    if isinstance(value, dict) and set(value) in ({"section_key"}, {"heading_path"}):
        item = next(iter(value.values()))
        if (
            isinstance(item, str)
            or isinstance(item, list)
            and all(isinstance(x, str) for x in item)
        ):
            return [value]
    return []


def accept_locations(raw, navigation, parsed):
    """Share the normal field aliases and resolver, but never accept page mutations."""
    text = _unfence(str(raw or ""))
    result = Selection(
        empty=bool(
            re.fullmatch(
                r"(?:none|n/?a|no (?:relevant|matching|supporting) sections?|"
                r"无|没有相关章节|未找到相关章节)[。.!\s]*",
                text,
                re.I,
            )
        )
    )
    if result.empty or not text:
        return result
    candidates = []
    if text.startswith(("{", "[")):
        try:
            value = repair_loads(text)
        except (ValueError, TypeError):
            value = None

        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if _field_name(key) == "section":
                        candidates.extend(
                            _safe_clues(
                                {key: item} if key in {"section_key", "heading_path"} else item
                            )
                        )
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        collect(item)
                    else:
                        candidates.extend(_safe_clues(item))

        collect(value)
    else:
        in_subject = False
        for line in text.splitlines():
            plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", line).strip()
            if heading := _HEADING.match(plain):
                in_subject = _field_name(heading[2]) == "section"
                continue
            if plain.startswith("|"):
                continue
            item = _ITEM.match(plain)
            body = item[1] if item else plain
            match = _FIELD.match(body)
            # A bare section:key is a location, not an unknown labelled field.
            if re.match(r"`?section:", body):
                candidates.append(body)
            elif match:
                in_subject = _field_name(match[1]) == "section"
                if in_subject and match[2]:
                    candidates.append(match[2])
            elif item or in_subject:
                candidates.append(body)
            elif body:
                # Bare exact titles are safe: the shared resolver still requires
                # a unique supplied chapter. Explanatory prose cannot mint a range.
                candidates.append(body)
        table_text = "\n".join(line for line in text.splitlines() if line.strip().startswith("|"))
        for row in _markdown_rows(table_text):
            candidates.extend(hint["value"] for hint in _hints(row) if hint["role"] == "subject")
    for candidate in candidates:
        for safe in _safe_clues(candidate):
            for clue in context_choices(safe, navigation):
                try:
                    ranges, _ = resolve_hint(clue, navigation, parsed)
                except ValueError:
                    result.unresolved.append(clue)
                    continue
                if ranges:
                    result.clues.append(clue)
                    result.ranges.extend(value for value in ranges if value not in result.ranges)
                    literal = _literal_nodes(clue, navigation) if isinstance(clue, str) else []
                    keys = (
                        [row["section_key"] for row in literal]
                        if literal
                        else _SECTION_KEY.findall(json.dumps(clue, ensure_ascii=False))
                    )
                    result.entries.append({"clue": clue, "keys": keys})
    if not result.ranges and not result.unresolved:
        result.unresolved.append(text[:500])
    return result


def source_messages_for(
    page, snapshot, source, parsed, settings, limits, *, reason="", details=None
):
    """Keep the exact planning P; add only task-specific navigation in the suffix."""
    context = json.loads(snapshot["context_json"])
    common = context["common_inputs"]
    target = {
        "kind": "page_sources",
        "page": {
            "key": page.key,
            "title": page.title,
            "kind": page.kind,
            "type": page.type,
            "purpose": page.purpose,
            "location_hints": page.location_hints,
        },
        "navigation_partial": bool(context["projection"].get("omitted_nodes")),
    }
    rows = details or []

    def build(rows):
        return plan_messages(
            empty_evidence(source, parsed),
            {},
            target,
            rows,
            common["existing_pages"],
            common["entity_types"],
            common["schema"],
            common["language"],
            common["existing_targets"],
            common["source_conditions"],
            subtask="page_sources",
            recovery=reason,
            planning_context=context,
        )

    messages = build(rows)
    if rows and not fits(messages, settings, limits):
        rows = [{**row, "summary": ""} for row in rows]
        messages = build(rows)
    # No silently sliced selection; the caller records an input-capacity omission.
    limits.request(settings["model"], messages, {})
    return messages
