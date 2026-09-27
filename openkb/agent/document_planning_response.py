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
from openkb.agent.document_planning_markdown import _HEADING, _markdown_rows
from openkb.agent.document_planning_pages import (
    DEFAULT_PURPOSE,
    distinct_scope,
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
    action_heading,
    add_annotation,
    annotation,
    deferred_suggestion,
    explicit_action,
    explicit_extension,
    field_semantics,
    inherit_classification,
    matching_suggestions,
    promote_deferred,
)
from openkb.sources import content_id

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
    promoted_suggestions: list[dict[str, str]] = field(default_factory=list)
    external_reference_hints: list[dict[str, Any]] = field(default_factory=list)

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


def _json_rows(
    value: Any, inherited_kind: str | None = None, action: str | None = None
) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for item in value for row in _json_rows(item, inherited_kind, action)]
    if not isinstance(value, dict):
        return []
    if any(_field_name(str(key)) in {"name", "title", "target"} for key in value) or (
        any(_field_name(str(key)) in {"kind", "type"} for key in value)
        and not {"pages", "page_changes", "create", "update", "concepts", "entities"} & value.keys()
    ):
        return [
            {
                **value,
                **({"group_kind": inherited_kind} if inherited_kind else {}),
                **({"group_action": action} if action else {}),
            }
        ]
    rows: list[dict[str, Any]] = []
    for key, item in value.items():
        if key in {"pages", "page_changes", "create", "update"}:
            rows.extend(
                _json_rows(item, inherited_kind, key if key in {"create", "update"} else action)
            )
        elif key in {"concepts", "entities"}:
            rows.extend(_json_rows(item, "concept" if key == "concepts" else "entity", action))
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
                group = next(
                    (
                        frame.get("key")
                        for frame in reversed(frames)
                        if frame.get("key") in {"concepts", "entities"}
                    ),
                    parent["key"],
                )
                action = next(
                    (
                        frame.get("key")
                        for frame in reversed(frames)
                        if frame.get("key") in {"create", "update"}
                    ),
                    None,
                )
                inherited = (
                    "concept" if group == "concepts" else "entity" if group == "entities" else None
                )
                rows.extend(_json_rows(value, inherited, action))
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


def _explicit_empty_sets(value):
    if isinstance(value, list):
        return not value
    if not isinstance(value, dict):
        return False
    collections = [
        item
        for key, item in value.items()
        if key in {"pages", "page_changes", "concepts", "entities", "create", "update"}
    ]
    return bool(collections) and all(_explicit_empty_sets(item) for item in collections)


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
        return rows, not rows and _explicit_empty_sets(value), truncated
    rows = _markdown_rows(content, truncated=truncated, batch_notes=batch_notes)
    no_pages = not rows and (
        bool(_NO_PAGES.search(content))
        or any(
            action_heading(match[2])[0] in {"create", "update", "notes", "skip", "defer"}
            for line in content.splitlines()
            if (match := _HEADING.match(line))
        )
    )
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
        for name in field_semantics(str(key)):
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
        for meaning in field_semantics(str(key)):
            role = roles.get(meaning)
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
    catalog_types: dict[str, str] | None = None,
    accepted: list[PagePlan] | None = None,
    default_entity_type: str | None = None,
    evidence: dict[str, Any] | None = None,
    source_identity: str | None = None,
    request_binding: dict[str, Any] | None = None,
    deferred: list[dict[str, Any]] | None = None,
    annotations: dict[str, Any] | None = None,
) -> PageAcceptance:
    """Retain recognizable organization suggestions; evidence is prepared before generation."""
    from openkb.agent.document_planning_bindings import bind_hints, validate_binding
    from openkb.agent.document_planning_locations import resolve_hint

    if request_binding is not None:
        validate_binding(request_binding, source_identity, request_binding["version_id"], parsed)

    batch_notes: list[str] = []
    rows, no_pages, truncated = extract_candidates(raw, batch_notes=batch_notes)
    result = PageAcceptance(no_pages=no_pages, truncated=truncated, batch_notes=batch_notes)
    known = {page.name: page for page in accepted or []}
    known_annotations = dict(annotations or {})
    explicit_pages = set()
    if not rows and not no_pages:
        result.rejected.append({"reason": "pages_unparseable", "candidate": str(raw or "")[:300]})
    for entry, original in enumerate(rows):
        row, conflicts = _normalize(original)
        notes = ["字段存在不同表达：" + field for field in conflicts]
        for field_name, value in row.items():
            if field_name == "notes":
                notes.extend(str(item) for item in (value if isinstance(value, list) else [value]))
            elif (
                field_name not in _ALIASES
                and field_name not in {"group_kind", "group_action"}
                and value
            ):
                notes.append(f"{field_name}：{value}")
        action_value = row.get("action", row.get("group_action"))
        if not action_value and row.get("target"):
            action_value = "update"
        action = explicit_action(action_value) if action_value else None
        if row.get("action") and action != row.get("group_action") and row.get("group_action"):
            notes.append(
                "行内动作覆盖分组动作：" + str(row["group_action"]) + " → " + str(row["action"])
            )
        update_target = ""
        if action == "update":
            label = (
                _first_text(row.get("target"))
                or _first_text(row.get("title"))
                or _first_text(row.get("name"))
            )
            matches = (
                [label]
                if label in existing_targets
                else [
                    path
                    for path in existing_targets
                    if normalized_name((catalog_titles or {}).get(path, ""))
                    == normalized_name(label)
                ]
            )
            if len(matches) == 1 and matches[0] in (allowed_update_targets or set()):
                update_target = matches[0]
                row.setdefault("title", (catalog_titles or {}).get(update_target) or label)
                row.setdefault(
                    "kind", "concept" if update_target.startswith("concepts/") else "entity"
                )
                if (catalog_types or {}).get(update_target):
                    row.setdefault("type", (catalog_types or {})[update_target])
            else:
                row.setdefault("title", label)
        title = _first_text(row.get("title")) or _first_text(row.get("name"))
        name = _first_text(row.get("name")) or title
        if extension := explicit_extension(row, title):
            row["extends"] = extension
        purpose = _first_text(row.get("purpose")) or DEFAULT_PURPOSE
        decision = _classification(
            row, original.get("group_kind"), entity_types, default_entity_type, notes
        )
        decision = inherit_classification(
            row, decision, title, purpose, known.values(), known_annotations
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
        hints = bind_hints(_hints(original), request_binding, parsed, notes)
        origin = {"response": content_id(str(raw)), "entry": entry}
        if request_binding is not None:
            origin.update({key: request_binding[key] for key in ("request", "window")})
        if action in {"skip", "notes"}:
            result.batch_notes.append(title + "：" + purpose + "；".join(notes))
            _filter_candidate(result, identity, "explicit_" + action, original)
            continue
        deferred_reason = (
            "unknown_action"
            if action_value and action is None
            else "explicit_defer"
            if action == "defer"
            else "update_target_unresolved"
            if action == "update" and not update_target
            else decision.reason
        )
        if extension:
            extension_matches = matching_suggestions(extension, known.values(), known_annotations)
            if (
                len(extension_matches) != 1
                or (
                    kind is not None
                    and (extension_matches[0].kind, extension_matches[0].type) != (kind, subtype)
                )
                or (
                    len(extension_matches) == 1
                    and distinct_scope(extension_matches[0], purpose, hints)
                )
            ):
                deferred_reason = deferred_reason or "extension_target_unresolved"

        if kind is None or deferred_reason:
            result.deferred_suggestions.append(
                deferred_suggestion(
                    original,
                    title,
                    purpose,
                    hints,
                    notes,
                    origin,
                    source_identity or getattr(parsed, "id", ""),
                    deferred_reason,
                )
            )
            continue
        proposed = update_target or _first_text(row.get("target"))
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
        if previous is not None and distinct_scope(previous, purpose, hints):
            page_key = "page:" + content_id((page_key, purpose))[:24]
            previous = next((page for page in known.values() if page.key == page_key), None)
        extension = _first_text(row.get("extends"))
        if not proposed and (extension or name == title):
            prior_suggestions = matching_suggestions(
                extension or title, known.values(), known_annotations
            )
            prior_suggestions = [
                page for page in prior_suggestions if not distinct_scope(page, purpose, hints)
            ]
            if len(prior_suggestions) == 1 and (
                prior_suggestions[0].kind,
                prior_suggestions[0].type,
            ) == (kind, subtype):
                previous = prior_suggestions[0]
                page_key = previous.key
            elif extension:
                notes.append("未确认补充对象或类别：" + extension)
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
        if row.get("references"):
            from openkb.agent.document_planning_semantics import reference_hints

            result.external_reference_hints.extend(
                reference_hints(row["references"], page_key, origin)
            )
        if action in {"create", "update"}:
            explicit_pages.add(page_key)
        add_annotation(result.annotations, page_key, annotation(original, decision, title, origin))
        add_annotation(known_annotations, page_key, annotation(original, decision, title, origin))
    for page in known.values():
        if (
            page.key in result.annotations
            and len(matching_suggestions(page.title, known.values(), known_annotations)) == 1
        ):
            already_promoted = {row["deferred_key"] for row in result.promoted_suggestions}
            promote_deferred(
                result,
                page,
                [
                    row
                    for row in (deferred or []) + result.deferred_suggestions
                    if row["key"] not in already_promoted
                    and (
                        row["reason"] not in {"explicit_defer", "conditional_recommendation"}
                        or page.key in explicit_pages
                    )
                ],
            )
    return result
