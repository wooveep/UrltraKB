"""Finite Markdown regions and tolerant closed-item extraction."""

import re
from typing import Any

from openkb.agent.document_planning_semantics import _ALIASES, _field_name, _label, action_heading

_FIELD = re.compile(r"^\s*(?:[-*+]\s*)?([\w\u4e00-\u9fff `/\-]+?)\s*[：:]\s*(.*?)\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、]\s+)(.*)$")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_EXPLANATION_HEADING = re.compile(
    r"^(?:notes?|qualifications?|limitations?)\b|"
    r"^(?:说明|限制|备注|补充说明)(?:$|[：:、/与及（( ])",
    re.I,
)
_EMPTY_GROUP_NOTICE = re.compile(
    r"[（(]\s*(?:未发现|没有|暂无|无)\s*(?:已有|既有)?\s*"
    r"(?:concepts?|entit(?:y|ies)|概念|实体|知识)?\s*(?:pages?|页面|页)\s*[）)]",
    re.I,
)
_INLINE_FIELD = re.compile(
    r"(?<![\w/])("
    + "|".join(
        re.escape(label)
        for label in sorted(
            {label for aliases in _ALIASES.values() for label in aliases}, key=len, reverse=True
        )
    )
    + r")\s*[：:]",
    re.I,
)


def _inline_fields(key: str, value: str) -> dict[str, str]:
    """Split explicit labelled fields, never the colon in an opaque section key."""
    if _field_name(key) != "purpose":
        return {key: value}
    masked = list(value)
    stack: list[str] = []
    closing = {"(": ")", "（": "）", "[": "]", "「": "」", "“": "”", '"': '"', "`": "`"}
    for index, char in enumerate(value):
        if stack:
            masked[index] = " "
            if char == stack[-1]:
                stack.pop()
            elif char in closing and stack[-1] not in {'"', "`", "”"}:
                stack.append(closing[char])
        elif char in closing:
            masked[index] = " "
            stack.append(closing[char])
    result = {}
    start = 0
    for match in _INLINE_FIELD.finditer("".join(masked)):
        before = value[: match.start()].rstrip()
        if not before or before[-1] not in ".。;；|—–":
            continue
        if match[1].casefold() == "section" and re.match(r"[\w-]", value[match.end() :]):
            continue
        result[key] = value[start : match.start()].strip()
        key, start = _field_name(match[1]) or match[1], match.end()
    result[key] = value[start:].strip()
    return result


def _markdown_rows(
    content: str, *, truncated: bool = False, batch_notes: list[str] | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    group: str | None = None
    explanatory = False
    action = None
    region_level = 2
    regions: list[tuple[int, str | None, str | None, bool]] = []
    current: dict[str, Any] | None = None
    heading_item = False
    pending_field: str | None = None
    current_indent = 0
    lines = content.splitlines()
    table_groups: dict[int, tuple[str | None, str | None, bool]] = {}
    ends: dict[int, int] = {}
    table_closed: dict[int, bool] = {}
    for line_index, line in enumerate(lines):
        if not line.strip():
            if not heading_item and current is not None and id(current) in ends:
                current = None
            continue
        if re.fullmatch(r"(?:[-*_]\s*){3,}", line.strip()):
            group, action, current, explanatory = None, None, None, False
            regions.clear()
            continue
        heading = _HEADING.match(line)
        if (
            not heading
            and line.strip(" *").endswith((":", "："))
            and _EXPLANATION_HEADING.match(line.strip(" *"))
        ):
            explanatory, group, current = True, None, None
            continue
        if heading:
            heading_item, pending_field = False, None
            label = heading.group(2).strip()
            level = len(heading.group(1))
            while regions and regions[-1][0] >= level:
                regions.pop()
            next_action, next_group = action_heading(label)
            if next_action or next_group:
                # Kind and action can live at different heading levels, in either order.
                parent = regions[-1] if regions else (0, None, None, False)
                action = next_action or parent[1]
                group = next_group or parent[2]
                region_level = level
                explanatory = parent[3] or action == "notes"
                regions.append((level, action, group, explanatory))
                current = None
            elif level <= region_level or _EXPLANATION_HEADING.match(_label(label)):
                group, action, current = None, None, None
                explanatory = bool(_EXPLANATION_HEADING.match(_label(label)))
                region_level = level
                regions.append((level, action, group, explanatory))
            elif not explanatory:
                heading_item = True
                labelled = re.fullmatch(r"(concept|entity|概念|实体)\s*[：:]\s*(.+)", label, re.I)
                name_heading = re.fullmatch(r"(?:concepts|entities)/[^/]+", label.strip(" `"))
                current = {
                    "name" if name_heading else "title": labelled[2].strip() if labelled else label,
                    **({"group_kind": group} if group else {}),
                    **({"group_action": action} if action else {}),
                }
            if explanatory and batch_notes is not None:
                batch_notes.append(line)
            continue
        if explanatory:
            if batch_notes is not None:
                batch_notes.append(line)
            table_groups[line_index] = (None, None, True)
            current = None
            continue
        if line.strip().startswith("|"):
            table_groups[line_index] = (group, action, False)
            continue
        item = _ITEM.match(line)
        body = item.group(1) if item else line.strip()
        body = re.sub(r"\*\*([^*]+)\*\*", r"\1", body)
        match = _FIELD.match(body)
        named_field = _field_name(match.group(1)) if match else None
        if item and group and not match and _EMPTY_GROUP_NOTICE.fullmatch(body):
            if batch_notes is not None:
                batch_notes.append(body)
            current = None
            continue
        if heading_item and current is not None and pending_field and item:
            previous = current.get(pending_field)
            values = previous if isinstance(previous, list) else [previous] if previous else []
            current[pending_field] = [*values, body]
            ends[id(current)] = line_index
            if not any(current is row for row in rows):
                rows.append(current)
            continue
        inline_purpose = re.fullmatch(
            r"(.+?)\s*[—–]\s*(?:purpose|用途|目的|说明)\s*[：:]\s*(.+)", body, re.I
        )
        if item and not named_field and inline_purpose:
            current = {
                "title": inline_purpose[1].strip(),
                **_inline_fields("purpose", inline_purpose[2].strip()),
            }
            typed = re.fullmatch(
                r"(.+?)\s*[—–]\s*(?:type|类型)\s*[：:]\s*([\w-]+)",
                current["title"],
                re.I,
            )
            if typed:
                current.update(title=typed[1].strip(), type=typed[2])
            elif group == "entity":
                typed = re.fullmatch(
                    r"(.+?)\s*[（(](?:(?:type|类型)\s*[：:]\s*)?([\w-]+)\s*[）)]",
                    current["title"],
                    re.I,
                )
                if typed:
                    current.update(title=typed[1].strip(), type=typed[2])
            if group:
                current["group_kind"] = group
            if action:
                current["group_action"] = action
            rows.append(current)
            ends[id(current)] = line_index
            continue
        if item and not named_field and " — " in body:
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
                if action:
                    current["group_action"] = action
                if group:
                    current["group_kind"] = group
                rows.append(current)
                ends[id(current)] = line_index
                continue
        if item and not named_field and body.count("|") >= 2:
            cells = [cell.strip(" `") for cell in body.split("|")]
            section = cells[2]
            if section.lower().startswith(("section_key:", "section key:", "章节:")):
                section = section.split(":", 1)[1].strip()
            current = {
                "name": cells[0],
                "type": cells[1],
                "section": section,
            }
            if action:
                current["group_action"] = action
            if group:
                current["group_kind"] = group
            rows.append(current)
            ends[id(current)] = line_index
            continue
        if item and not match and (body.endswith(("。", ".", "！", "!", "；", ";"))):
            if batch_notes is not None:
                batch_notes.append(body)
            current = None
            continue
        indent = len(line) - len(line.lstrip())
        if (
            item
            and not heading_item
            and (not match or _field_name(match.group(1)) in {"name", "title", "target"})
            and (current is None or indent <= current_indent)
        ):
            heading_item, pending_field = False, None
            current_indent = indent
            if group:
                current = {
                    "group_kind": group,
                    **({"group_action": action} if action else {}),
                    **({"title": body} if not match else {}),
                }
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
            current_indent = indent
        if match and current is not None:
            if action:
                current["group_action"] = action
            key = match.group(1).strip()
            value = match.group(2).strip()
            if _field_name(key) == "title":
                # Collapse only a confirmed display decoration, not two different titles.
                displayed = re.sub(
                    r"^(?:concept|entity|概念|实体)\s*[：:]\s*",
                    "",
                    current.get("title", ""),
                    flags=re.I,
                )
                if displayed.strip(" *`").casefold() == value.strip(" *`").casefold():
                    current["title"] = value
            current.update(_inline_fields(key, value))
            pending_field = (
                key
                if not value
                and _field_name(key)
                in {"section", "context", "related", "references", "notes", "purpose"}
                else None
            )
            ends[id(current)] = line_index
            if not any(current is row for row in rows):
                rows.append(current)
        elif heading_item and current is not None and not item:
            previous = current.get("purpose", "")
            current["purpose"] = (previous + "\n\n" + body).strip()
            ends[id(current)] = line_index
            if not any(current is row for row in rows):
                rows.append(current)
        elif current is None and batch_notes is not None:
            batch_notes.append(body)
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
            table_group, table_action, blocked = table_groups.get(index, (None, None, False))
            if blocked:
                continue
            if table_action:
                row["group_action"] = table_action
            if table_group:
                row["group_kind"] = table_group
            if row:
                rows.append(row)
                ends[id(row)] = row_index
                table_closed[id(row)] = row_line.rstrip().endswith("|") and len(cells) == len(
                    columns
                )
    rows.sort(key=lambda row: ends.get(id(row), 0))
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
