"""Finite Markdown regions and tolerant closed-item extraction."""

import re
from typing import Any

from openkb.agent.document_planning_semantics import _field_name, _label, action_heading

_FIELD = re.compile(r"^\s*(?:[-*+]\s*)?([\w\u4e00-\u9fff `/\-]+?)\s*[：:]\s*(.*?)\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、]\s+)(.*)$")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_EXPLANATION_HEADING = re.compile(
    r"^(?:notes?|qualifications?|limitations?)\b|"
    r"^(?:说明|限制|备注|补充说明)(?:$|[：:、/与及（( ])",
    re.I,
)


def _markdown_rows(
    content: str, *, truncated: bool = False, batch_notes: list[str] | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    group: str | None = None
    explanatory = False
    action = None
    region_level = 2
    current: dict[str, Any] | None = None
    current_indent = 0
    lines = content.splitlines()
    table_groups: dict[int, tuple[str | None, str | None, bool]] = {}
    ends: dict[int, int] = {}
    table_closed: dict[int, bool] = {}
    for line_index, line in enumerate(lines):
        if not line.strip():
            if current is not None and id(current) in ends:
                current = None
            continue
        if re.fullmatch(r"(?:[-*_]\s*){3,}", line.strip()):
            group, action, current, explanatory = None, None, None, False
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
            label = heading.group(2).strip()
            level = len(heading.group(1))
            next_action, next_group = action_heading(label)
            if next_action or next_group:
                if level <= region_level or next_action:
                    action = next_action
                group = next_group
                region_level = level
                explanatory = action == "notes"
                current = None
            elif level <= region_level or _EXPLANATION_HEADING.match(_label(label)):
                group, action, current = None, None, None
                explanatory = bool(_EXPLANATION_HEADING.match(_label(label)))
                region_level = level
            elif not explanatory:
                current = {
                    "title": label,
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
                if action:
                    current["group_action"] = action
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
            and (not match or _field_name(match.group(1)) in {"name", "title", "target"})
            and (current is None or indent <= current_indent)
        ):
            current_indent = indent
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
            current_indent = indent
        if match and current is not None:
            if action:
                current["group_action"] = action
            key = match.group(1).strip()
            current[key] = match.group(2).strip()
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
