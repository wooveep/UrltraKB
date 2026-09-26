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
    resolve_location,
    section_contribution,
    within_target,
)
from openkb.agent.document_planning_pages import DEFAULT_PURPOSE, merge_page, select_page_path
from openkb.agent.document_planning_pages import _slug as _slug

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
_ALIASES = {
    "name": {"name", "page", "名称", "页面", "页面名称", "页面名称/标题", "page name"},
    "title": {"title", "page title", "标题", "页面标题"},
    "kind": {"kind", "类别", "分类", "页面类别", "category"},
    "type": {"type", "类型", "页面类型", "实体类型", "entity type"},
    "section": {
        "section",
        "sections",
        "主体章节",
        "依据章节",
        "章节定位",
        "章节路径",
        "来源章节",
        "原文章节",
        "定位",
        "section location",
        "来源位置",
        "source location",
        "章节",
        "主体范围",
        "位置",
        "section_key",
        "heading_path",
        "subject_ranges",
    },
    "context": {"context", "必要上下文", "前提章节", "相关章节", "context_sections"},
    "purpose": {"purpose", "用途", "说明", "注意事项"},
    "references": {"references", "外部参考", "参考资料", "external_references"},
    "target": {"target", "target_key", "目标页面"},
}


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

    @property
    def usable(self) -> bool:
        return bool(self.pages or self.filtered or self.no_pages)


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


def _label(value: str) -> str:
    return " ".join(value.strip(" *`\t\r\n").lower().replace("_", " ").split())


def _field_name(value: str) -> str | None:
    key = _label(value)
    exact = next(
        (
            name
            for name, aliases in _ALIASES.items()
            if key in aliases or key.replace(" ", "_") in aliases
        ),
        None,
    )
    if exact:
        return exact
    if "section key" in key or "heading path" in key or "章节位置" in key:
        return "section"
    if "标题路径" in key or key.startswith("章节/"):
        return "section"
    if key.startswith("page title"):
        return "title"
    if key.startswith("说明/"):
        return "purpose"
    return None


def _kind(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip().lower()
    if label.startswith("concept") or label.startswith("概念"):
        return "concept"
    if label.startswith("entit") or label.startswith("实体"):
        return "entity"
    return None


def _entity_type(value: Any, allowed: list[str]) -> str | None:
    original = str(value or "").strip().lower()
    nested = re.fullmatch(r"(?:entity|实体)\s*[（(]([^）)]+)[）)]", original)
    label = nested.group(1).strip() if nested else re.sub(r"\s*[（(].*$", "", original)
    aliases = {
        "产品": "product",
        "作品": "work",
        "工作": "work",
        "人物": "person",
        "组织": "organization",
        "地点": "place",
        "事件": "event",
        "其他": "other",
    }
    normalized = aliases.get(label, label)
    return normalized if normalized in allowed else None


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
                **({"kind": inherited_kind} if inherited_kind and "kind" not in value else {}),
            }
        ]
    rows: list[dict[str, Any]] = []
    for key, item in value.items():
        if key in {"pages", "page_changes", "create", "update"}:
            rows.extend(_json_rows(item, inherited_kind))
        elif key in {"concepts", "entities"}:
            rows.extend(_json_rows(item, "concept" if key == "concepts" else "entity"))
    return rows


def _markdown_rows(content: str, *, truncated: bool = False) -> list[dict[str, Any]]:
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
                group = next_group
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
        if item and (not match or _field_name(match.group(1)) in {"name", "title"}):
            if group:
                current = {"group_kind": group}
            elif match:
                current = {}
            else:
                current = None
        elif item and match and current is None:
            current = {}
        if match and current is not None:
            key = match.group(1).strip()
            current[key] = match.group(2).strip()
            ends[id(current)] = line_index
            if not any(current is row for row in rows):
                rows.append(current)
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


def extract_candidates(raw: Any) -> tuple[list[dict[str, Any]], bool, bool]:
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
    rows = _markdown_rows(content, truncated=truncated)
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


def _display_title(value: Any) -> str:
    title = str(value or "").strip()
    for opening, closing in (("「", "」"), ("『", "』"), ("《", "》")):
        if title.startswith(opening) and title.endswith(closing):
            return title[1:-1].strip()
    return title


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
) -> PageAcceptance:
    """Accept entries independently, resolving only proven source locations."""
    rows, no_pages, truncated = extract_candidates(raw)
    result = PageAcceptance(no_pages=no_pages, truncated=truncated)
    known = {page.name: page for page in accepted or []}
    if not rows and not no_pages:
        result.rejected.append({"reason": "pages_unparseable", "candidate": str(raw or "")[:300]})
    for original in rows:
        row, conflicts = _normalize(original)
        group_kind = _kind(original.get("group_kind"))
        title = _display_title(row.get("title") or row.get("name"))
        name = _display_title(row.get("name") or title)
        explicit_kind = _kind(row.get("kind"))
        type_from_kind = _entity_type(row.get("kind"), entity_types)
        supplied_type = _entity_type(row.get("type"), entity_types)
        type_kind = _kind(row.get("type"))
        kind = (
            explicit_kind
            or group_kind
            or type_kind
            or ("entity" if supplied_type or type_from_kind else None)
        )
        category_conflict = bool({"kind", "type"} & set(conflicts))
        if category_conflict:
            categories = {
                category
                for field in ("kind", "type", "group_kind")
                for value in (row[field] if field in conflicts else [row.get(field)])
                if (
                    category := _kind(value)
                    or ("entity" if _entity_type(value, entity_types) else None)
                )
            }
            kind = next(iter(categories)) if len(categories) == 1 else None
        identity = candidate_identity(
            row,
            kind,
            title,
            name,
            reliable=not ({"name", "title"} & set(conflicts))
            and isinstance(row.get("name") or row.get("title"), str)
            and isinstance(row.get("title") or row.get("name"), str),
        )
        candidate_key = identity["candidate_key"]
        candidate_name_key = identity.get("candidate_name_key")
        if not conflicts and _summary_category(row, kind):
            _filter_candidate(result, identity, "summary_placeholder", original)
            continue
        try:
            if conflicts:
                if category_conflict and kind is None:
                    raise ValueError("conflicting_kind")
                raise ValueError("conflicting_field:" + ",".join(conflicts))
            if identity["identity_kind"] == "opaque":
                raise ValueError("missing_title")
            if row.get("kind") and explicit_kind is None and type_from_kind is None:
                raise ValueError("unknown_kind")
            if group_kind and explicit_kind and group_kind != explicit_kind:
                raise ValueError("conflicting_kind")
            if type_kind and any(
                prior != type_kind for prior in (group_kind, explicit_kind) if prior
            ):
                raise ValueError("conflicting_kind")
            if supplied_type and "concept" in (group_kind, explicit_kind):
                raise ValueError("conflicting_kind")
            if kind is None:
                raise ValueError("unknown_kind")
            entity_type = supplied_type or type_from_kind or default_entity_type
            if kind == "entity" and entity_type not in entity_types:
                raise ValueError("unknown_entity_type")
            placeholder = row.get("section")
            if isinstance(placeholder, str) and re.fullmatch(
                r"(?:referenced by title only|未随本文提供|未提供正文|未附正文|引用处|"
                r".+[（(](?:引用处|未随本文提供|referenced by title only)[）)])",
                placeholder.strip(" `"),
                re.I,
            ):
                try:
                    resolve_location(placeholder, navigation, [], parsed, evidence)
                except ValueError:
                    _filter_candidate(result, identity, "reference_hint", original)
                    continue
            cited_without_body = any(
                re.search(
                    rf"《{re.escape(title)}》[^。.!?]{{0,100}}"
                    r"(?:未随本文提供|未提供正文|未附正文)",
                    str(block.get("text", "")),
                )
                for block in (evidence or {}).get("blocks", [])
                if isinstance(block, dict)
            )
            if cited_without_body and not any(node.get("title") == title for node in navigation):
                # An explicitly unsupplied cited document is a reference, even
                # when the model labels it as a concept rather than a work.
                _filter_candidate(result, identity, "unsupplied_reference", original)
                continue
            page_key = "page:" + candidate_key[:24]
            previous = next((page for page in known.values() if page.key == page_key), None)
            path, target_name = select_page_path(
                kind=kind,
                title=title,
                name=name,
                proposed=row.get("target"),
                existing=existing_targets,
                allowed=allowed_update_targets or set(),
                catalog=catalog_titles or {},
                accepted=known,
                previous=previous,
            )
            notes: list[str] = []
            try:
                ranges, scope = resolve_location(
                    row.get("section"), navigation, target, parsed, evidence, notes=notes
                )
            except ValueError as exc:
                if str(exc) != "subject_outside_target" or previous is None:
                    raise
                ranges, scope = resolve_location(
                    row.get("section"), navigation, [], parsed, evidence, mode="context"
                )
            supplied_target = (
                section_contribution(target, target, parsed, evidence)
                if evidence is not None and any("subject_ranges" in node for node in navigation)
                else target
            )
            outside_echo = not within_target(ranges, supplied_target, parsed)
            if outside_echo and not (
                previous is not None and within_target(ranges, previous.subject_ranges, parsed)
            ):
                raise ValueError("subject_outside_target")
            contexts: list[RangeValue] = []
            if row.get("context"):
                for clue in context_choices(row["context"], navigation):
                    try:
                        located, _ = resolve_location(
                            clue, navigation, [], parsed, evidence, notes=notes, mode="context"
                        )
                        contexts.extend(value for value in located if value not in contexts)
                    except ValueError:
                        notes.append("未定位必要上下文：" + str(clue)[:120])
            page = PagePlan(
                key=page_key,
                kind=kind,
                name=path,
                title=title,
                purpose=str(row.get("purpose") or DEFAULT_PURPOSE),
                target=target_name,
                type=str(entity_type) if kind == "entity" else None,
                subject_ranges=ranges,
                context_ranges=contexts,
                planning_notes=notes + ([str(row["references"])] if row.get("references") else []),
                scope_resolution=scope,
            )
            if previous is not None:
                changed = merge_page(
                    previous, page, add_subject=not outside_echo and scope != "target_fallback"
                )
                _filter_candidate(
                    result,
                    identity,
                    "merged_page" if changed else "accepted_echo",
                    original,
                    resolved=scope != "target_fallback",
                )
                continue
            known[path] = page
            result.pages.append(page)
            result.resolved_candidates[candidate_key] = candidate_name_key
        except (TypeError, ValueError) as exc:
            result.rejected.append(
                {
                    "reason": str(exc),
                    "candidate": json.dumps(original, ensure_ascii=False)[:300],
                    **identity,
                }
            )
    return result
