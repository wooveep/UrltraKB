"""Best-effort acceptance of document-planning text without model-owned paths."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from openkb.agent.document_plan import PagePlan, RangeValue
from openkb.agent.document_planning_candidates import candidate_identity
from openkb.agent.document_planning_locations import (
    context_choices,
    labelled_key_paths,
)
from openkb.agent.document_planning_pages import (
    DEFAULT_PURPOSE,
    merge_page,
    normalized_name,
    select_page_path,
    suggestion_key,
)
from openkb.agent.document_planning_pages import _slug as _slug
from openkb.agent.document_planning_semantics import (
    _ALIASES,
    _classification,
    _field_name,
    _first_text,
    _kind,
    _label,
    annotation,
    deferred_suggestion,
)
from openkb.sources import content_id

_FIELD = re.compile(r"^\s*(?:[-*+]\s*)?([\w\u4e00-\u9fff `/\-]+?)\s*[：:]\s*(.*?)\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、]\s+)(.*)$")
_HEADING = re.compile(r"^\s*(#{2,6})\s+(.+?)\s*$")
_FENCE = re.compile(r"^```(?:markdown|md|json)?\s*\n([\s\S]*?)\n```\s*$", re.I)
_NO_PAGES = re.compile(
    r"(?:无需|不需要|没有必要|无须).{0,12}(?:新|创建|新增)?.{0,8}(?:页面|知识页)"
    r"|no (?:new )?pages? (?:needed|recommended|(?:is |are )?warranted)",
    re.I,
)
_PLACEHOLDER = re.compile(
    r"^(?:待补充|无内容|暂无|n/?a|none|placeholder|todo|无法判断|没有足够信息)[。.!\s]*$", re.I
)


@dataclass
class OverviewAcceptance:
    text: str = ""
    reason: str | None = None
    truncated: bool = False


@dataclass
class PageAcceptance:
    pages: list[PagePlan] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    filtered: list[dict[str, str]] = field(default_factory=list)
    resolved_candidates: dict[str, str | None] = field(default_factory=dict)
    no_pages: bool = False
    truncated: bool = False
    deferred_suggestions: list[dict[str, Any]] = field(default_factory=list)
    annotations: dict[str, Any] = field(default_factory=dict)
    batch_notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.pages or self.deferred_suggestions or self.filtered or self.no_pages)


def _unfence(text: str, *, preserve_trailing: bool = False) -> str:
    content = text.lstrip() if preserve_trailing else text.strip()
    if content.startswith('"'):
        try:
            decoded = json.loads(content)
            if isinstance(decoded, str):
                content = decoded.lstrip() if preserve_trailing else decoded.strip()
        except ValueError:
            pass
    match = _FENCE.fullmatch(content.strip())
    if match:
        return match.group(1) + "\n\n" if preserve_trailing else match.group(1).strip()
    return content


def accept_overview(raw: Any) -> OverviewAcceptance:
    """Accept readable Markdown; a length stop keeps only complete paragraphs."""
    content = _unfence(str(raw or ""))
    if content.startswith(("{", "[")):
        try:
            payload = json.loads(content)
        except ValueError:
            return OverviewAcceptance(reason="overview_unusable")
        if isinstance(payload, dict):
            values = [
                payload[key]
                for key in ("overview", "summary", "text")
                if isinstance(payload.get(key), str)
            ]
            if len(values) == 1:
                content = _unfence(values[0])
            else:
                return OverviewAcceptance(reason="overview_unusable")
        else:
            return OverviewAcceptance(reason="overview_unusable")
    truncated = getattr(raw, "finish_reason", None) == "length"
    if truncated:
        boundary = content.rfind("\n\n")
        content = content[:boundary].strip() if boundary >= 0 else ""
        if content.count("```") % 2:
            content = content[: content.rfind("```")].strip()
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    useful = [line for line in lines if not line.startswith("#") and line not in {"-", "*", "+"}]
    if not useful or all(_PLACEHOLDER.fullmatch(line) for line in useful):
        return OverviewAcceptance(reason="overview_unusable", truncated=truncated)
    return OverviewAcceptance(content, "overview_truncated" if truncated else None, truncated)


def _summary_category(row: dict[str, Any], page_kind: str | None) -> bool:
    return page_kind is None and any(
        isinstance(row.get(key), str)
        and _label(row[key]) in {"summary", "overview", "摘要", "概览", "{{summary}}"}
        for key in ("kind", "type")
    )


def _filter_candidate(
    result: PageAcceptance,
    identity: dict[str, str],
    reason: str,
    original: dict[str, Any],
    *,
    resolved: bool = True,
) -> None:
    result.filtered.append(
        {**identity, "reason": reason, "candidate": json.dumps(original, ensure_ascii=False)[:300]}
    )
    if resolved:
        result.resolved_candidates[identity["candidate_key"]] = identity.get("candidate_name_key")


def _json_rows(value: Any, inherited_kind: str | None = None) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for item in value for row in _json_rows(item, inherited_kind)]
    if not isinstance(value, dict):
        return []
    if any(_field_name(str(key)) in {"name", "title"} for key in value) or (
        any(_field_name(str(key)) in {"kind", "type"} for key in value)
        and not {"pages", "page_changes", "create", "update", "concepts", "entities"} & value.keys()
    ):
        return [
            {
                **value,
                **({"group_kind": inherited_kind} if inherited_kind else {}),
            }
        ]
    rows: list[dict[str, Any]] = []
    for key, item in value.items():
        if key in {"pages", "page_changes", "create", "update"}:
            rows.extend(_json_rows(item, inherited_kind))
        elif key in {"concepts", "entities"}:
            rows.extend(_json_rows(item, "concept" if key == "concepts" else "entity"))
    return rows


def _markdown_rows(
    content: str, *, truncated: bool = False, batch_notes: list[str] | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    group: str | None = None
    current: dict[str, Any] | None = None
    lines = content.splitlines()
    table_groups: dict[int, str | None] = {}
    ends: dict[int, int] = {}
    table_closed: dict[int, bool] = {}
    for line_index, line in enumerate(lines):
        if not line.strip():
            if current is not None and id(current) in ends:
                current = None
            continue
        heading = _HEADING.match(line)
        if heading:
            label = heading.group(2).strip()
            next_group = _kind(label) or _kind(label.removesuffix("页面"))
            if next_group:
                group = label
                current = None
            elif len(heading.group(1)) == 2:
                group = None
                current = None
            else:
                current = {"title": label, **({"group_kind": group} if group else {})}
            continue
        if line.strip().startswith("|"):
            table_groups[line_index] = group
            continue
        item = _ITEM.match(line)
        body = item.group(1) if item else line.strip()
        body = re.sub(r"\*\*([^*]+)\*\*", r"\1", body)
        if item and " — " in body:
            parts = [part.strip() for part in body.split(" — ", 2)]
            if len(parts) >= 2 and parts[0] and parts[1]:
                type_label = re.split(r"[,，;；]", parts[1], maxsplit=1)[0].strip()
                inline_section = parts[1][len(type_label) :].lstrip(" ,，;；")
                section = inline_section or (parts[2] if len(parts) > 2 else "")
                current = {"name": parts[0], "type": type_label}
                if inline_section and len(parts) > 2:
                    current["purpose"] = parts[2]
                if section:
                    current["section"] = section
                if group:
                    current["group_kind"] = group
                rows.append(current)
                ends[id(current)] = line_index
                continue
        if item and body.count("|") >= 2:
            cells = [cell.strip(" `") for cell in body.split("|")]
            section = cells[2]
            if section.lower().startswith(("section_key:", "section key:", "章节:")):
                section = section.split(":", 1)[1].strip()
            current = {
                "name": cells[0],
                "type": cells[1],
                "section": section,
            }
            if group:
                current["group_kind"] = group
            rows.append(current)
            ends[id(current)] = line_index
            continue
        match = _FIELD.match(body)
        if item and not match and (body.endswith(("。", ".", "！", "!", "；", ";"))):
            if batch_notes is not None:
                batch_notes.append(body)
            current = None
            continue
        if item and (not match or _field_name(match.group(1)) in {"name", "title"}):
            if group:
                current = {"group_kind": group, **({"title": body} if not match else {})}
                if not match:
                    rows.append(current)
                    ends[id(current)] = line_index
            elif match:
                current = {}
            else:
                current = None
        elif (
            item
            and match
            and current is None
            and _field_name(match.group(1)) in {"kind", "type", "section"}
        ):
            current = {}
        if match and current is not None:
            key = match.group(1).strip()
            current[key] = match.group(2).strip()
            ends[id(current)] = line_index
            if not any(current is row for row in rows):
                rows.append(current)
        elif not item and current is None and batch_notes is not None:
            batch_notes.append(line.strip())
    # Tables are independent of surrounding lists and column order.
    for index, line in enumerate(lines[:-2]):
        if not line.strip().startswith("|") or not re.fullmatch(r"[\s|:\-]+", lines[index + 1]):
            continue
        columns = [cell.strip() for cell in line.strip().strip("|").split("|")]
        for row_index, row_line in enumerate(lines[index + 2 :], start=index + 2):
            if not row_line.strip().startswith("|"):
                break
            cells = [cell.strip() for cell in row_line.strip().strip("|").split("|")]
            row = {key: value for key, value in zip(columns, cells) if key and value}
            table_group = table_groups.get(index)
            if table_group:
                row["group_kind"] = table_group
            if row:
                rows.append(row)
                ends[id(row)] = row_index
                table_closed[id(row)] = row_line.rstrip().endswith("|") and len(cells) == len(
                    columns
                )
    if not truncated:
        return rows
    return [
        row
        for row in rows
        if table_closed.get(id(row), False)
        or (
            id(row) not in table_closed
            and any(
                not line.strip()
                or _HEADING.match(line)
                or _ITEM.match(line)
                or line.strip().startswith("|")
                or line.strip() == "```"
                for line in lines[ends[id(row)] + 1 :]
            )
        )
    ]


def _closed_json_pages(content: str) -> list[dict[str, Any]]:
    """Salvage complete direct page entries, never nested page fields."""
    frames: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    quoted = escaped = False
    string_start = 0
    for offset, char in enumerate(content):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
                following = content[offset + 1 :].lstrip()
                if frames and frames[-1]["kind"] == "{" and following.startswith(":"):
                    try:
                        frames[-1]["pending_key"] = json.loads(content[string_start : offset + 1])
                    except ValueError:
                        pass
            continue
        if char == '"':
            quoted = True
            string_start = offset
        elif char in "{[":
            parent = frames[-1] if frames else None
            key = parent.pop("pending_key", None) if parent and parent["kind"] == "{" else None
            page_items = char == "[" and (
                not frames
                or (
                    key in {"pages", "page_changes", "concepts", "entities", "create", "update"}
                    and (
                        len(frames) == 1
                        or len(frames) == 2
                        and frames[1].get("key")
                        in {"pages", "page_changes", "concepts", "entities"}
                    )
                )
            )
            frames.append({"kind": char, "start": offset, "key": key, "page_items": page_items})
        elif char in "}]":
            if not frames or frames[-1]["kind"] != ("{" if char == "}" else "["):
                return []
            frame = frames.pop()
            if char != "}":
                continue
            try:
                value = json.loads(content[frame["start"] : offset + 1])
            except ValueError:
                continue
            if not frames:
                return _json_rows(value)
            parent = frames[-1]
            if parent["kind"] == "[" and parent["page_items"]:
                group = parent["key"]
                inherited = (
                    "concept" if group == "concepts" else "entity" if group == "entities" else None
                )
                rows.extend(_json_rows(value, inherited))
    return rows


def _json_structure_closed(content: str) -> bool:
    stack: list[str] = []
    quoted = escaped = False
    for char in content:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack.pop() != ("{" if char == "}" else "["):
                return False
    return not quoted and not stack


def extract_candidates(
    raw: Any, *, batch_notes: list[str] | None = None
) -> tuple[list[dict[str, Any]], bool, bool]:
    content = _unfence(str(raw or ""), preserve_trailing=True)
    truncated = getattr(raw, "finish_reason", None) == "length"
    if content.startswith(("{", "[")):
        if truncated or not _json_structure_closed(content):
            return _closed_json_pages(content), False, True
        try:
            value = json.loads(content)
        except ValueError:
            try:
                from json_repair import repair_json

                value = repair_json(content, return_objects=True)
            except (ImportError, ValueError, TypeError):
                value = None
        rows = _json_rows(value)
        return rows, False, truncated
    rows = _markdown_rows(content, truncated=truncated, batch_notes=batch_notes)
    no_pages = bool(_NO_PAGES.search(content)) and not rows
    return rows, no_pages, truncated


def _normalize(row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, list[Any]] = {}
    for key, value in row.items():
        name = _field_name(str(key)) or str(key).strip()
        if isinstance(value, str):
            value = value.strip()
            if name in {"kind", "type"}:
                value = value.strip(" *`\t\r\n").lower()
                if value in {"concept", "概念", "entity", "实体"}:
                    value = _kind(value)
        if name == "section":
            label = _label(str(key))
            if any(part in label for part in ("heading path", "标题路径", "章节路径")) and any(
                part in label for part in ("section key", "章节键")
            ):
                value = labelled_key_paths(value)
            elif label in {"heading path", "标题路径", "章节路径"} and value not in (None, ""):
                value = {"heading_path": value}
        if (name not in _ALIASES or value not in (None, "")) and value not in values.setdefault(
            name, []
        ):
            values[name].append(value)
    result: dict[str, Any] = {}
    conflicts = []
    for name, choices in sorted(values.items()):
        if not choices:
            continue
        if len(choices) > 1:
            if name == "section":
                result[name] = choices
            else:
                conflicts.append(name)
                result[name] = sorted(choices, key=lambda value: json.dumps(value, sort_keys=True))
        else:
            result[name] = choices[0]
    return result, conflicts


def _hints(row: dict[str, Any]) -> list[dict[str, Any]]:
    roles = {"section": "subject", "context": "context", "related": "related"}
    hints = []
    for key, value in row.items():
        role = roles.get(_field_name(str(key)) or "")
        if role and value not in (None, "", []):
            hint = {"role": role, "value": value}
            if hint not in hints:
                hints.append(hint)
    return hints


def accept_pages(
    raw: Any,
    *,
    navigation: list[dict[str, Any]],
    target: list[RangeValue],
    parsed: Any,
    entity_types: list[str],
    existing_targets: set[str],
    allowed_update_targets: set[str] | None = None,
    catalog_titles: dict[str, str] | None = None,
    accepted: list[PagePlan] | None = None,
    default_entity_type: str | None = None,
    evidence: dict[str, Any] | None = None,
    source_identity: str | None = None,
) -> PageAcceptance:
    """Retain recognizable organization suggestions; evidence is prepared before generation."""
    from openkb.agent.document_planning_locations import resolve_hint

    batch_notes: list[str] = []
    rows, no_pages, truncated = extract_candidates(raw, batch_notes=batch_notes)
    result = PageAcceptance(no_pages=no_pages, truncated=truncated, batch_notes=batch_notes)
    known = {page.name: page for page in accepted or []}
    if not rows and not no_pages:
        result.rejected.append({"reason": "pages_unparseable", "candidate": str(raw or "")[:300]})
    for entry, original in enumerate(rows):
        row, conflicts = _normalize(original)
        notes = ["字段存在不同表达：" + field for field in conflicts]
        for field_name, value in row.items():
            if field_name == "notes":
                notes.extend(str(item) for item in (value if isinstance(value, list) else [value]))
            elif field_name not in _ALIASES and field_name != "group_kind" and value:
                notes.append(f"{field_name}：{value}")
        title = _first_text(row.get("title")) or _first_text(row.get("name"))
        name = _first_text(row.get("name")) or title
        decision = _classification(
            row, original.get("group_kind"), entity_types, default_entity_type, notes
        )
        kind, subtype = decision.kind, decision.subtype
        identity = candidate_identity(row, kind, title, name, reliable=bool(title))
        if _summary_category(
            row,
            _kind(row.get("kind"))
            or _kind(original.get("group_kind"))
            or ("entity" if subtype else None),
        ):
            _filter_candidate(result, identity, "summary_placeholder", original)
            continue
        if not title:
            result.rejected.append(
                {
                    **identity,
                    "reason": "dropped_item",
                    "candidate": json.dumps(original, ensure_ascii=False)[:300],
                }
            )
            continue
        placeholder = _first_text(row.get("section"))
        missing_reference = bool(
            re.fullmatch(
                r"(?:referenced by title only|未随本文提供|未提供正文|未附正文|引用处|"
                r".+[（(](?:引用处|未随本文提供|referenced by title only)[）)])",
                placeholder,
                re.I,
            )
        )
        cited_without_body = any(
            re.search(
                rf"《{re.escape(title)}》[^。.!?]{{0,100}}(?:未随本文提供|未提供正文|未附正文)",
                str(block.get("text", "")),
            )
            for block in (evidence or {}).get("blocks", [])
            if isinstance(block, dict)
        )
        if (missing_reference or cited_without_body) and not any(
            node.get("title") == title for node in navigation
        ):
            _filter_candidate(result, identity, "unsupplied_reference", original)
            continue
        hints = _hints(original)
        purpose = _first_text(row.get("purpose")) or DEFAULT_PURPOSE
        origin = {"response": content_id(str(raw)), "entry": entry}
        if kind is None:
            result.deferred_suggestions.append(
                deferred_suggestion(
                    original,
                    title,
                    purpose,
                    hints,
                    notes,
                    origin,
                    source_identity or getattr(parsed, "id", ""),
                    decision.reason,
                )
            )
            continue
        proposed = _first_text(row.get("target"))
        folder = "concepts/" if kind == "concept" else "entities/"
        allowed = allowed_update_targets or set()
        if proposed and (
            proposed not in existing_targets
            or proposed not in allowed
            or not proposed.startswith(folder)
        ):
            notes.append("未采用无法确认的更新意向：" + proposed)
            proposed = ""
        if not proposed:
            matches = [
                path
                for path, label in (catalog_titles or {}).items()
                if path.startswith(folder) and normalized_name(label) == normalized_name(title)
            ]
            if len(matches) == 1 and matches[0] in allowed and matches[0] in existing_targets:
                proposed = matches[0]
        page_key = suggestion_key(
            str(source_identity or getattr(parsed, "id", "")), kind, subtype, name, title, proposed
        )
        previous = next((page for page in known.values() if page.key == page_key), None)
        try:
            path, target_name = select_page_path(
                kind=kind,
                title=title,
                name=name,
                proposed=proposed or None,
                existing=existing_targets,
                allowed=allowed,
                catalog=catalog_titles or {},
                accepted=known,
                previous=previous,
            )
        except ValueError as exc:
            result.rejected.append(
                {
                    **identity,
                    "reason": str(exc),
                    "candidate": json.dumps(original, ensure_ascii=False)[:300],
                }
            )
            continue
        subjects: list[RangeValue] = []
        contexts: list[RangeValue] = []
        scope = None
        for hint in hints:
            if hint["role"] == "related":
                continue
            for clue in context_choices(hint["value"], navigation):
                try:
                    located, resolution = resolve_hint(clue, navigation, parsed, evidence, notes)
                    destination = subjects if hint["role"] == "subject" else contexts
                    destination.extend(value for value in located if value not in destination)
                    if hint["role"] == "subject":
                        scope = resolution
                except ValueError:
                    notes.append("待取证线索：" + str(clue)[:120])
        page = PagePlan(
            key=page_key,
            kind=kind,
            type=subtype,
            name=path,
            title=title,
            target=target_name,
            purpose=purpose,
            subject_ranges=subjects,
            context_ranges=contexts,
            location_hints=hints,
            state="pending_evidence",
            scope_resolution=scope,
            planning_notes=notes + ([str(row["references"])] if row.get("references") else []),
        )
        if previous is not None:
            changed = merge_page(previous, page, add_subject=True)
            _filter_candidate(
                result, identity, "merged_page" if changed else "accepted_echo", original
            )
        else:
            known[path] = page
            result.pages.append(page)
            result.resolved_candidates[identity["candidate_key"]] = identity.get(
                "candidate_name_key"
            )
        result.annotations[page_key] = annotation(original, decision, title, origin)
    return result
