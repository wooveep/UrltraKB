"""Semantic labels and conservative classification of planning suggestions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan import PagePlan
from openkb.sources import content_id

_ALIASES = {
    "name": {"name", "page", "名称", "页面", "页面名称", "页面名称/标题", "page name"},
    "title": {"title", "page title", "标题", "页面标题"},
    "kind": {"kind", "类别", "分类", "页面类别", "category"},
    "type": {"type", "类型", "页面类型", "实体类型", "entity type"},
    "section": {
        "section",
        "sections",
        "selection",
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
    "context": {"context", "必要上下文", "前提章节", "context_sections"},
    "related": {
        "related",
        "相关章节",
        "keywords",
        "关键词",
        "相关线索",
        "位置线索",
        "定位线索",
        "定位提示",
        "location",
        "location clues",
        "location hints",
        "reference",
        "参考",
    },
    "purpose": {
        "purpose",
        "用途",
        "用途说明",
        "说明",
        "建议依据",
        "description",
        "rationale",
        "用途或参考",
    },
    "notes": {"notes", "note", "备注", "补充说明", "注意事项"},
    "references": {"references", "外部参考", "参考资料", "external_references"},
    "target": {"target", "target_key", "目标页面"},
    "extends": {"extends", "extend", "补充已有建议", "补充建议", "补充"},
}


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
    plain = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", key)
    plain = re.sub(r"^(?:建议(?:的)?|suggested\s+)", "", plain)
    if plain != key and (field_name := _field_name(plain)):
        return field_name
    # Composite display labels preserve their recognizable field, without
    # requiring one fixed heading spelling or guessing at arbitrary prose.
    parts = re.split(r"\s*[/／]\s*", key)
    if len(parts) > 1:
        fields = {_field_name(part) for part in parts} - {None}
        if fields <= {"name", "title"} and fields:
            return "title"
        if len(fields) == 1:
            return next(iter(fields))
        if fields <= {"section", "context", "related"} and fields:
            return "related"
    if re.fullmatch(
        r"(?:建议(?:的)?|页面|概念|实体|suggested |concept |entity |page )+"
        r"(?:名称|标题|name|title)",
        key,
    ):
        return "title"
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
    label = _label(value)
    label = re.sub(r"^(?:建议(?:的)?|suggested\s+|recommended\s+)", "", label)
    if re.fullmatch(r"(?:concepts?|概念)(?:\s*pages?|页(?:面)?)?\s*(?:[（(].*[）)])?", label):
        return "concept"
    if re.fullmatch(r"(?:entit(?:y|ies)|实体)(?:\s*pages?|页(?:面)?)?\s*(?:[（(].*[）)])?", label):
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
        "人员": "person",
        "组织": "organization",
        "地点": "place",
        "事件": "event",
        "其他": "other",
    }
    normalized = aliases.get(label, label)
    return normalized if normalized in allowed else None


def _display_title(value: Any) -> str:
    title = str(value or "").strip()
    wrappers = (("**", "**"), ("__", "__"), ("`", "`"), ("「", "」"), ("『", "』"), ("《", "》"))
    while True:
        for opening, closing in wrappers:
            if (
                len(title) > len(opening) + len(closing)
                and title.startswith(opening)
                and title.endswith(closing)
            ):
                title = title[len(opening) : -len(closing)].strip()
                break
        else:
            break
    return title


def _first_text(value: Any) -> str:
    values = value if isinstance(value, list) else [value]
    return next(
        (_display_title(item) for item in values if isinstance(item, str) and item.strip()), ""
    )


def _classification(row, group, types, default, notes):
    """Resolve only supplied meaning; uncertainty never mints a concept page."""
    labels = [
        (key, item)
        for key in ("kind", "type")
        for item in (row.get(key) if isinstance(row.get(key), list) else [row.get(key)])
        if isinstance(item, str) and item.strip()
    ]
    supplied = labels + ([("group", group)] if group else [])
    kinds = {kind for _, value in supplied if (kind := _kind(value))}
    subtypes = {subtype for _, value in supplied if (subtype := _entity_type(value, types))}
    if subtypes:
        kinds.add("entity")
    if len(kinds) > 1 or len(subtypes) > 1:
        return Classification(None, None, "conflict", "classification_conflict")
    if not kinds:
        return Classification(None, None, "unknown", "classification_unknown")
    kind = next(iter(kinds))
    subtype = next(iter(subtypes), None)
    basis = "explicit" if any(_kind(v) or _entity_type(v, types) for _, v in labels) else "group"
    if kind == "entity" and subtype is None:
        unknown_subtype = any(key == "type" and _kind(value) is None for key, value in labels)
        if default not in types or unknown_subtype:
            return Classification(None, None, basis, "entity_type_unknown")
        subtype, basis = default, "configured_default"
        notes.append("实体细类采用配置默认值：" + str(default))
    return Classification(kind, subtype, basis)


@dataclass(frozen=True)
class Classification:
    kind: str | None
    subtype: str | None
    basis: str
    reason: str | None = None


def labels_for(row):
    labels = {}
    for key, value in row.items():
        name = "group_kind" if key == "group_kind" else _field_name(key)
        if name in {"kind", "type", "group_kind"} and value:
            labels[name] = value if isinstance(value, str) else str(value)
    return labels


def annotation(row, decision, title, origin):
    return {
        "labels": [labels_for(row)],
        "basis": [decision.basis],
        "aliases": [title],
        "origins": [origin],
    }


def deferred_suggestion(row, title, purpose, hints, notes, origin, source, reason):
    return {
        "key": "suggestion:" + content_id((source, title, purpose))[:24],
        "title": title,
        "purpose": purpose,
        "labels": labels_for(row),
        "location_hints": hints,
        "notes": notes,
        "origins": [origin],
        "reason": reason,
    }


def record_semantics(state, result):
    deferred = state.setdefault("deferred_suggestions", [])
    promoted = state.setdefault("promoted_suggestions", [])
    for row in result.promoted_suggestions:
        if row not in promoted:
            promoted.append(row)
        deferred[:] = [item for item in deferred if item["key"] != row["deferred_key"]]
    for incoming in result.deferred_suggestions:
        previous = next((row for row in deferred if row["key"] == incoming["key"]), None)
        if previous is None:
            deferred.append(incoming)
        else:
            for field, value in incoming["labels"].items():
                if field not in previous["labels"]:
                    previous["labels"][field] = value
                elif previous["labels"][field] != value:
                    old = previous["labels"][field]
                    values = (old if isinstance(old, list) else [old]) + (
                        value if isinstance(value, list) else [value]
                    )
                    previous["labels"][field] = list(dict.fromkeys(values))
            if incoming["reason"] == "classification_conflict":
                previous["reason"] = incoming["reason"]
            for field in ("location_hints", "notes", "origins"):
                previous[field].extend(row for row in incoming[field] if row not in previous[field])
    annotations = state.setdefault("suggestion_annotations", {})
    for key, incoming in result.annotations.items():
        previous = annotations.setdefault(key, {field: [] for field in incoming})
        for field, values in incoming.items():
            previous[field].extend(row for row in values if row not in previous[field])
    state.setdefault("batch_notes", []).extend(
        note for note in result.batch_notes if note not in state.get("batch_notes", [])
    )


def add_annotation(annotations, key, incoming):
    previous = annotations.setdefault(key, {field: [] for field in incoming})
    for field, values in incoming.items():
        previous[field].extend(row for row in values if row not in previous[field])


def inherit_classification(row, decision, title, purpose, pages, annotations):
    """Absence can inherit known meaning; supplied unknown labels cannot."""
    from openkb.agent.document_planning_pages import DEFAULT_PURPOSE

    if decision.kind or any(row.get(key) for key in ("kind", "type", "group_kind")):
        return decision
    label = _first_text(row.get("extends")) or title
    matches = matching_suggestions(label, pages, annotations)
    if len(matches) == 1 and (
        purpose == DEFAULT_PURPOSE or purpose == matches[0].purpose or row.get("extends")
    ):
        return Classification(matches[0].kind, matches[0].type, "inherited")
    return decision


def matching_suggestions(
    label: str, pages: Iterable[PagePlan], annotations: dict[str, Any]
) -> list[PagePlan]:
    from openkb.agent.document_planning_pages import normalized_name

    return [
        page
        for page in pages
        if any(
            normalized_name(name) == normalized_name(label)
            for name in [page.title] + annotations.get(page.key, {}).get("aliases", [])
        )
    ]


def promote_deferred(result, page, candidates):
    from openkb.agent.document_planning_pages import DEFAULT_PURPOSE, normalized_name

    matches = [
        row for row in candidates if normalized_name(row["title"]) == normalized_name(page.title)
    ]
    if len(matches) != 1:
        return
    row = matches[0]
    if row["reason"] == "classification_conflict" or (
        row["purpose"] != DEFAULT_PURPOSE and page.purpose not in {row["purpose"], DEFAULT_PURPOSE}
    ):
        return
    if page.purpose == DEFAULT_PURPOSE:
        page.purpose = row["purpose"]
    for field, additions in (
        ("location_hints", row["location_hints"]),
        ("planning_notes", row["notes"]),
    ):
        existing = getattr(page, field)
        existing.extend(item for item in additions if item not in existing)
    add_annotation(
        result.annotations,
        page.key,
        {
            "labels": [row["labels"]],
            "basis": ["promoted"],
            "aliases": [row["title"]],
            "origins": row["origins"],
        },
    )
    result.promoted_suggestions.append({"deferred_key": row["key"], "page_key": page.key})
    result.deferred_suggestions[:] = [
        item for item in result.deferred_suggestions if item["key"] != row["key"]
    ]


def validate_semantics(metadata):
    """Validate the one persisted representation used by state, plan and report."""
    if metadata.get("planning_semantics") != "tolerant-quality-v1":
        raise ValueError("Invalid planning semantics")
    deferred = metadata.get("deferred_suggestions")
    annotations = metadata.get("suggestion_annotations")
    if not isinstance(deferred, list) or not isinstance(annotations, dict):
        raise ValueError("Invalid planning suggestions")
    if not isinstance(metadata.get("batch_notes", []), list) or any(
        not isinstance(value, str) for value in metadata.get("batch_notes", [])
    ):
        raise ValueError("Invalid planning explanations")
    if not isinstance(metadata.get("promoted_suggestions", []), list) or any(
        not isinstance(row, dict)
        or set(row) != {"deferred_key", "page_key"}
        or any(not isinstance(value, str) for value in row.values())
        for row in metadata.get("promoted_suggestions", [])
    ):
        raise ValueError("Invalid promoted suggestions")
    for row in deferred:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"key", "title", "purpose", "labels", "location_hints", "notes", "origins", "reason"}
            or not all(
                isinstance(row[k], str) and row[k] for k in ("key", "title", "purpose", "reason")
            )
        ):
            raise ValueError("Invalid deferred suggestion")
        from openkb.agent.document_plan import location_hints

        location_hints(row["location_hints"])
        if not isinstance(row["notes"], list) or not all(
            isinstance(note, str) for note in row["notes"]
        ):
            raise ValueError("Invalid deferred notes")
        _validate_origins(row["origins"])
        _validate_labels(row["labels"])
    for key, row in annotations.items():
        if (
            not isinstance(key, str)
            or not isinstance(row, dict)
            or set(row) != {"labels", "basis", "aliases", "origins"}
        ):
            raise ValueError("Invalid suggestion annotation")
        for field in ("basis", "aliases"):
            if not isinstance(row[field], list) or not all(isinstance(v, str) for v in row[field]):
                raise ValueError("Invalid suggestion annotation text")
        if not isinstance(row["labels"], list):
            raise ValueError("Invalid suggestion labels")
        for labels in row["labels"]:
            _validate_labels(labels)
        _validate_origins(row["origins"])


def _validate_labels(value):
    if not isinstance(value, dict) or set(value) - {"kind", "type", "group_kind"}:
        raise ValueError("Invalid suggestion labels")
    if any(
        not isinstance(v, (str, list))
        or isinstance(v, list)
        and not all(isinstance(t, str) for t in v)
        for v in value.values()
    ):
        raise ValueError("Invalid classification expression")


def _validate_origins(value):
    if not isinstance(value, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("response"), str)
        or type(row.get("entry")) is not int
        or row["entry"] < 0
        for row in value
    ):
        raise ValueError("Invalid suggestion origin")
