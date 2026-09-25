"""Best-effort acceptance of document-planning text without model-owned paths."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from openkb.agent.document_plan import PagePlan, RangeValue, range_intervals
from openkb.sources import content_id

_FIELD = re.compile(r"^\s*(?:[-*+]\s*)?([\w\u4e00-\u9fff /-]+?)\s*[：:]\s*(.*?)\s*$")
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
    "name": {"name", "名称", "页面", "页面名称", "页面名称/标题", "page name"},
    "title": {"title", "标题", "页面标题"},
    "kind": {"kind", "类别", "分类", "页面类别", "category"},
    "type": {"type", "类型", "页面类型", "实体类型", "entity type"},
    "section": {
        "section",
        "sections",
        "主体章节",
        "依据章节",
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
    no_pages: bool = False
    truncated: bool = False


def _unfence(text: str) -> str:
    content = text.strip()
    if content.startswith('"'):
        try:
            decoded = json.loads(content)
            if isinstance(decoded, str):
                content = decoded.strip()
        except ValueError:
            pass
    match = _FENCE.fullmatch(content)
    return match.group(1).strip() if match else content


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


def _field_name(value: str) -> str | None:
    key = value.strip(" *`\t").lower().replace("_", " ")
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


def _json_rows(value: Any, inherited_kind: str | None = None) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for item in value for row in _json_rows(item, inherited_kind)]
    if not isinstance(value, dict):
        return []
    if any(_field_name(str(key)) in {"name", "title"} for key in value):
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


def _markdown_rows(content: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    group: str | None = None
    current: dict[str, Any] | None = None
    lines = content.splitlines()
    table_groups: dict[int, str | None] = {}
    for line_index, line in enumerate(lines):
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
                section_match = re.search(r"section:[a-zA-Z0-9_-]+", body)
                path = re.search(
                    r"(?:heading path|章节路径)\s*:\s*([^,，;；（(—]+)",
                    body,
                    flags=re.I,
                )
                section = (
                    section_match.group(0)
                    if section_match
                    else path.group(1).strip()
                    if path
                    else parts[2]
                    if len(parts) == 3
                    else ""
                )
                current = {"name": parts[0], "type": type_label}
                if section:
                    current["section"] = section
                if group:
                    current["group_kind"] = group
                rows.append(current)
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
            continue
        match = _FIELD.match(body)
        if item and (not match or _field_name(match.group(1)) in {"name", "title"}):
            if group:
                current = {"group_kind": group}
            elif match:
                current = {}
            else:
                current = None
        if match and current is not None:
            key = _field_name(match.group(1))
            if key:
                current[key] = match.group(2).strip()
                if current not in rows:
                    rows.append(current)
    # Tables are independent of surrounding lists and column order.
    for index, line in enumerate(lines[:-2]):
        if not line.strip().startswith("|") or not re.fullmatch(r"[\s|:\-]+", lines[index + 1]):
            continue
        columns = [_field_name(cell) for cell in line.strip().strip("|").split("|")]
        for row_line in lines[index + 2 :]:
            if not row_line.strip().startswith("|"):
                break
            cells = [cell.strip() for cell in row_line.strip().strip("|").split("|")]
            row = {key: value for key, value in zip(columns, cells) if key and value}
            table_group = table_groups.get(index)
            if table_group:
                row["group_kind"] = table_group
            if row:
                rows.append(row)
    return rows


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
                        frames[-1]["pending_key"] = json.loads(
                            content[string_start : offset + 1]
                        )
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
            frames.append(
                {"kind": char, "start": offset, "key": key, "page_items": page_items}
            )
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
                    "concept"
                    if group == "concepts"
                    else "entity"
                    if group == "entities"
                    else None
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
    content = _unfence(str(raw or ""))
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
    rows = _markdown_rows(content)
    if truncated and rows:
        # An incomplete final list item can only affect that item.
        if not content.endswith("\n\n"):
            rows = rows[:-1]
    no_pages = bool(_NO_PAGES.search(content)) and not rows
    return rows, no_pages, truncated


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        name = _field_name(str(key)) or (key if key in _ALIASES else None)
        if name and value not in (None, ""):
            result[name] = value
    return result


def _location(
    value: Any,
    navigation: list[dict[str, Any]],
    target: list[RangeValue],
    parsed: Any,
    evidence: dict[str, Any] | None = None,
) -> tuple[list[RangeValue], str]:
    if value is None or value == "":
        if not target:
            raise ValueError("missing_location")
        return target, "target_fallback"
    if isinstance(value, list) and all(isinstance(item, (list, dict)) for item in value):
        supplied = {
            block["id"]: block["order"]
            for block in (evidence or {}).get("blocks", [])
            if isinstance(block, dict) and isinstance(block.get("id"), str)
        }
        resolved: list[RangeValue] = []
        for item in value:
            if isinstance(item, dict) and "section_key" in item:
                selected_context, _ = _location(
                    item["section_key"], navigation, target, parsed, evidence
                )
                resolved.extend(selected_context)
                continue
            if isinstance(item, dict) and {"from_block", "through_block"} <= set(item):
                first = supplied.get(item["from_block"])
                last = supplied.get(item["through_block"])
                if first is None or last is None or first > last:
                    raise ValueError("unknown_location")
                item = [first, last + 1]
            elif isinstance(item, dict) and "block" in item:
                index = supplied.get(item["block"])
                if index is None:
                    raise ValueError("unknown_location")
                item = {
                    "block_index": index,
                    "start_char": item.get("start_char"),
                    "end_char": item.get("end_char"),
                }
            range_intervals(item, parsed, "planned page")
            resolved.append(item)
        return resolved, "explicit_range"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        value = " / ".join(value)
    clue = str(value).strip()
    section_keys = re.findall(r"section:[A-Za-z0-9_-]+", clue)
    if section_keys:
        selected: list[RangeValue] = []
        for section_key in dict.fromkeys(section_keys):
            matches = [node for node in navigation if node.get("section_key") == section_key]
            if len(matches) != 1:
                raise ValueError("unknown_location")
            ranges = matches[0].get("original_ranges")
            if ranges is None and isinstance(matches[0].get("original_range"), list):
                ranges = [matches[0]["original_range"]]
            if not isinstance(ranges, list) or not ranges:
                raise ValueError("unknown_location")
            selected.extend(ranges)
        for selected_range in selected:
            range_intervals(selected_range, parsed, "planned section")
        return selected, "section"
    if clue.isdigit():
        raise ValueError("ambiguous_numeric_location")
    candidates = []
    for node in navigation:
        path = node.get("heading_path", [])
        labels = {node.get("section_key"), " / ".join(path), " > ".join(path)}
        if path:
            labels.add(path[-1])
        if clue in labels:
            candidates.append(node)
    if len(candidates) != 1:
        if candidates:
            raise ValueError("ambiguous_location")
        if " / " in clue:
            parts = [part.strip() for part in clue.split(" / ") if part.strip()]
            combined: list[RangeValue] = []
            if len(parts) > 1:
                for part in parts:
                    located, _ = _location(part, navigation, target, parsed, evidence)
                    combined.extend(value for value in located if value not in combined)
                return combined, "section"
        raise ValueError("unknown_location")
    node = candidates[0]
    originals = node.get("original_ranges")
    if originals is None and isinstance(node.get("original_range"), list):
        originals = [node["original_range"]]
    if not isinstance(originals, list) or not originals:
        raise ValueError("unknown_location")
    for original in originals:
        range_intervals(original, parsed, "planned section")
    return originals, "section"


def _within_target(ranges: list[RangeValue], target: list[RangeValue], parsed: Any) -> bool:
    if not ranges:
        return False
    allowed = [
        interval for value in target for interval in range_intervals(value, parsed, "target")
    ]
    return all(
        any(
            index == known_index and left <= start and end <= right
            for known_index, left, right in allowed
        )
        for value in ranges
        for index, start, end in range_intervals(value, parsed, "planned page")
    )


def _slug(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).lower()
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:90]
    if not ascii_slug:
        return "topic-" + content_id(title)[:12]
    return ascii_slug + ("-" + content_id(title)[:12] if not normalized.isascii() else "")


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
        row = _normalize(original)
        if str(row.get("type") or "").strip().startswith(("摘要", "{{SUMMARY}}")):
            result.filtered.append(
                {"reason": "summary_placeholder", "candidate": str(original)[:300]}
            )
            continue
        if any(
            phrase in str(row.get(field) or "").lower()
            for field in ("section", "purpose")
            for phrase in ("referenced by title only", "未随本文提供", "引用处")
        ):
            result.filtered.append({"reason": "reference_hint", "candidate": str(original)[:300]})
            continue
        group_kind = _kind(original.get("group_kind"))
        title = _display_title(row.get("title") or row.get("name"))
        name = _display_title(row.get("name") or title)
        try:
            if not title or not name:
                raise ValueError("missing_title")
            explicit_kind = _kind(row.get("kind"))
            type_from_kind = _entity_type(row.get("kind"), entity_types)
            supplied_type = _entity_type(row.get("type"), entity_types)
            type_kind = _kind(row.get("type"))
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
            kind = (
                explicit_kind
                or group_kind
                or _kind(row.get("type"))
                or ("entity" if supplied_type or type_from_kind else None)
            )
            if kind is None:
                raise ValueError("unknown_kind")
            entity_type = supplied_type or type_from_kind or default_entity_type
            if kind == "entity" and entity_type not in entity_types:
                raise ValueError("unknown_entity_type")
            cited_without_body = any(
                re.search(
                    rf"《{re.escape(title)}》[^。.!?]{{0,100}}"
                    r"(?:未随本文提供|未提供正文|未附正文)",
                    str(block.get("text", "")),
                )
                for block in (evidence or {}).get("blocks", [])
                if isinstance(block, dict)
            )
            if cited_without_body and not any(
                node.get("title") == title for node in navigation
            ):
                # An explicitly unsupplied cited document is a reference, even
                # when the model labels it as a concept rather than a work.
                result.filtered.append({"reason": "unsupplied_reference", "candidate": title[:300]})
                continue
            folder = "concepts" if kind == "concept" else "entities"
            path = f"{folder}/{_slug(name)}"
            if row.get("target"):
                proposed = str(row["target"])
                if (
                    proposed not in existing_targets
                    or proposed not in (allowed_update_targets or set())
                    or not proposed.startswith(folder + "/")
                ):
                    raise ValueError("unknown_target")
                path = proposed
            elif catalog_titles:
                label = unicodedata.normalize("NFKC", title).casefold().strip()
                matches = [
                    target_path
                    for target_path, catalog_title in catalog_titles.items()
                    if target_path.startswith(folder + "/")
                    and unicodedata.normalize("NFKC", catalog_title).casefold().strip()
                    == label
                ]
                if len(matches) == 1 and matches[0] in (allowed_update_targets or set()):
                    path = matches[0]
                elif path in existing_targets:
                    path += "-new-" + content_id((kind, name))[:10]
            elif path in existing_targets:
                path += "-new-" + content_id((kind, name))[:10]
                if path in existing_targets:
                    raise ValueError("new_target_conflict")
            ranges, scope = _location(row.get("section"), navigation, target, parsed, evidence)
            if not _within_target(ranges, target, parsed):
                if any(
                    page.kind == kind
                    and _display_title(page.title) == title
                    and page.name == path
                    and _within_target(ranges, page.subject_ranges, parsed)
                    for page in known.values()
                ):
                    # Later windows may echo an already saved page by its
                    # section key; that does not create a new outside target.
                    result.filtered.append({"reason": "accepted_echo", "candidate": title[:300]})
                    continue
                raise ValueError("subject_outside_target")
            contexts: list[RangeValue] = []
            notes: list[str] = []
            if row.get("context"):
                context_clues = (
                    [row["context"]]
                    if isinstance(row["context"], list)
                    and all(isinstance(item, (list, dict)) for item in row["context"])
                    else re.split(r"[;；\n]", str(row["context"]))
                )
                for clue in context_clues:
                    if isinstance(clue, list) or isinstance(clue, str) and clue.strip():
                        try:
                            located, _ = _location(
                                clue.strip() if isinstance(clue, str) else clue,
                                navigation,
                                target,
                                parsed,
                                evidence,
                            )
                            contexts.extend(located)
                        except ValueError:
                            notes.append("未定位必要上下文：" + str(clue)[:120])
            previous = known.get(path)
            if previous:
                if previous.kind != kind or previous.title != title:
                    raise ValueError("accepted_page_conflict")
                if scope == "target_fallback":
                    # A repeated name without a located new section is an echo,
                    # not proof that the prior page owns this whole window.
                    result.filtered.append({"reason": "accepted_echo", "candidate": title[:300]})
                    continue
                before = len(previous.subject_ranges)
                for value in ranges:
                    if value not in previous.subject_ranges:
                        previous.subject_ranges.append(value)
                result.filtered.append(
                    {
                        "reason": (
                            "accepted_echo"
                            if len(previous.subject_ranges) == before
                            else "merged_page"
                        ),
                        "candidate": title[:300],
                    }
                )
                if previous.scope_resolution == "target_fallback":
                    previous.scope_resolution = "target_fallback"
                continue
            target_name = path if path in existing_targets else ""
            page = PagePlan(
                key="page:" + content_id((kind, name, ranges))[:24],
                kind=kind,
                name=path,
                title=title,
                purpose=str(row.get("purpose") or "根据已选原文整理本主题"),
                target=target_name,
                type=str(entity_type) if kind == "entity" else None,
                subject_ranges=ranges,
                context_ranges=contexts,
                planning_notes=notes + ([str(row["references"])] if row.get("references") else []),
                scope_resolution=scope,
            )
            known[path] = page
            result.pages.append(page)
        except (TypeError, ValueError) as exc:
            result.rejected.append(
                {"reason": str(exc), "candidate": json.dumps(original, ensure_ascii=False)[:300]}
            )
    return result
