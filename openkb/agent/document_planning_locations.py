"""Resolve supplied source locations without guessing unavailable evidence."""

from __future__ import annotations

import re
from typing import Any, Literal

from openkb.agent.document_plan import RangeValue, range_intervals
from openkb.agent.document_planning_projection import _compact_intervals

_PATH_LABEL = r"(?:heading[ _]path|章节路径|标题路径)\s*[：:]\s*"
_SECTION_KEY = re.compile(r"(?<![\w:-])section:[A-Za-z0-9_-]+(?![\w:-])")


def heading_path(value: str | list[str]) -> list[str]:
    """Remove only display wrappers, independently for every path segment."""
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("`") and value.endswith("`") and value.count("`") == 2:
            value = value[1:-1]
    parts = re.split(r"\s*[>→›]\s*|\s+/\s+", value) if isinstance(value, str) else value
    result = []
    for part in parts:
        part = part.strip()
        for left, right in (("**", "**"), ("`", "`"), ("「", "」"), ("『", "』"), ('"', '"')):
            if part.startswith(left) and part.endswith(right) and len(part) > len(left + right):
                part = part[len(left) : -len(right)].strip()
        result.append(part)
    return result


def _key_choices(value: str) -> list[str]:
    keys = list(_SECTION_KEY.finditer(value))
    if len(keys) < 2 or any(
        not re.fullmatch(r"[\s`、,，;；]+", value[a.end() : b.start()])
        for a, b in zip(keys, keys[1:])
    ):
        return [value]
    cuts = [0, *(key.start() for key in keys[1:]), len(value)]
    return [value[a:b].strip(" `、,，;；\n") for a, b in zip(cuts, cuts[1:])]


def _literal_nodes(clue: str, navigation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = []
    for node in navigation:
        path = node.get("heading_path", [])
        labels = {node.get("section_key"), node.get("title")}
        labels.update(separator.join(path) for separator in (" / ", " > ", " → ") if path)
        if path:
            labels.add(path[-1])
        if (
            clue in labels
            or heading_path(clue) == path
            or (len(heading_path(clue)) == 1 and heading_path(clue)[0] in labels)
        ):
            matches.append(node)
    return matches


def section_contribution(
    originals: list[RangeValue], target: list[RangeValue], parsed: Any, evidence: dict[str, Any]
) -> list[RangeValue]:
    """Intersect source sections with T and actual W, retaining exact character slices."""
    supplied = []
    for block in evidence.get("blocks", []):
        if not isinstance(block, dict) or type(block.get("order")) is not int:
            continue
        ref = block.get("reference") or {}
        start = ref.get("start", 0)
        value = {
            "block_index": block["order"],
            "start_char": start,
            "end_char": ref.get("end", start + len(block["text"])),
        }
        supplied.extend(range_intervals(value, parsed, "supplied planning evidence"))
    targets = [item for value in target for item in range_intervals(value, parsed, "target")]
    intervals = [
        (index, max(start, left, visible_start), min(end, right, visible_end))
        for value in originals
        for index, start, end in range_intervals(value, parsed, "section")
        for other, left, right in targets
        if other == index
        for visible, visible_start, visible_end in supplied
        if visible == index and max(start, left, visible_start) < min(end, right, visible_end)
    ]
    return _compact_intervals(parsed, intervals)


def _node_ranges(
    node: dict[str, Any], parsed: Any, mode: str, notes: list[str] | None
) -> list[RangeValue]:
    ranges = node.get("original_ranges")
    if ranges is None and isinstance(node.get("original_range"), list):
        ranges = [node["original_range"]]
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("unknown_location")
    for value in ranges:
        range_intervals(value, parsed, "planned section")
    if mode == "subject" and "subject_ranges" in node:
        selected = node["subject_ranges"]
        if not selected:
            raise ValueError("subject_outside_target")
        if selected and notes is not None and not within_target(ranges, selected, parsed):
            note = "章节在本窗的片段：" + str(node.get("title") or node.get("section_key"))
            if note not in notes:
                notes.append(note)
        return selected
    return ranges


def _location_choices(clue: str, *, separators: str = ";；\n") -> list[str]:
    """Split selections without interpreting punctuation inside path annotations."""
    parts = []
    start = depth = 0
    for index, char in enumerate(clue):
        if char in "(（":
            depth += 1
        elif char in ")）":
            depth = max(0, depth - 1)
        elif char in separators and depth == 0:
            parts.append(clue[start:index].strip())
            start = index + 1
    parts.append(clue[start:].strip())
    return [part for part in parts if part]


def _has_section_key(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_section_key(item) for item in value)
    if isinstance(value, dict):
        return _has_section_key(value.get("section_key"))
    return isinstance(value, str) and bool(
        re.fullmatch(r"section:[A-Za-z0-9_-]+", value.strip(" `"))
    )


def _is_path_annotation(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"heading_path"}) or (
        isinstance(value, str) and bool(re.match(_PATH_LABEL, value.strip(" `"), re.I))
    )


def labelled_key_paths(value: Any) -> Any:
    """Decode cells explicitly labelled as both section keys and display paths."""
    if not isinstance(value, str):
        return value
    choices: list[Any] = []
    for part in _location_choices(value):
        match = re.fullmatch(r"`?(section:[A-Za-z0-9_-]+)`?(?:\s+(.+))?", part)
        if match:
            choices.append({"section_key": match.group(1), "heading_path": match.group(2)})
        elif re.search(r"section:[A-Za-z0-9_-]+", part):
            choices.append(part)
        elif choices and isinstance(choices[-1], dict) and not choices[-1].get("heading_path"):
            choices[-1]["heading_path"] = part
        else:
            choices.append(part)
    return choices


def resolve_location(
    value: Any,
    navigation: list[dict[str, Any]],
    target: list[RangeValue],
    parsed: Any,
    evidence: dict[str, Any] | None = None,
    *,
    notes: list[str] | None = None,
    mode: Literal["subject", "context"] = "subject",
) -> tuple[list[RangeValue], str]:
    def locate(clue: Any) -> tuple[list[RangeValue], str]:
        return resolve_location(clue, navigation, target, parsed, evidence, notes=notes, mode=mode)

    def annotation(selected: list[RangeValue], clue: Any, primary: Any) -> None:
        try:
            selected, _ = resolve_location(
                primary, navigation, [], parsed, evidence, mode="context"
            )
            described, _ = resolve_location(clue, navigation, [], parsed, evidence, mode="context")
        except ValueError:
            return  # A labelled display annotation is not another selection.
        if notes is not None and not (
            within_target(described, selected, parsed)
            and within_target(selected, described, parsed)
        ):
            notes.append("章节键与路径说明不一致：" + str(clue)[:120])

    if value is None or value == "":
        if not target:
            raise ValueError("missing_location")
        return target, "target_fallback"
    if isinstance(value, dict):
        if "section_key" in value:
            if not isinstance(value["section_key"], str) or not value["section_key"].strip():
                raise ValueError("unknown_location")
            selected, scope = locate(value["section_key"])
            if value.get("heading_path"):
                annotation(selected, value["heading_path"], value["section_key"])
            return selected, scope
        if "heading_path" in value:
            if not value["heading_path"]:
                raise ValueError("unknown_location")
            return locate(value["heading_path"])
        supplied = {
            block["id"]: block["order"]
            for block in (evidence or {}).get("blocks", [])
            if isinstance(block, dict) and isinstance(block.get("id"), str)
        }
        item: Any = value
        if {"from_block", "through_block"} <= set(value):
            if not all(isinstance(value[key], str) for key in ("from_block", "through_block")):
                raise ValueError("unknown_location")
            first, last = supplied.get(value["from_block"]), supplied.get(value["through_block"])
            if first is None or last is None or first > last:
                raise ValueError("unknown_location")
            item = [first, last + 1]
        elif "block" in value:
            if not isinstance(value["block"], str) or value["block"] not in supplied:
                raise ValueError("unknown_location")
            item = {
                "block_index": supplied[value["block"]],
                "start_char": value.get("start_char"),
                "end_char": value.get("end_char"),
            }
        range_intervals(item, parsed, "planned page")
        return [item], "explicit_range"
    if isinstance(value, list):
        if not value:
            return [], "explicit_range"
        if any(item is None or item == "" for item in value):
            raise ValueError("unknown_location")
        if len(value) == 2 and all(type(item) is int for item in value):
            range_intervals(value, parsed, "planned page")
            return [value], "explicit_range"
        if all(isinstance(item, str) for item in value) and any(
            node.get("heading_path", [])[-len(value) :] == value for node in navigation
        ):
            return locate(" / ".join(value))
        combined: list[RangeValue] = []
        scopes = []
        comments = (
            [item for item in value if _is_path_annotation(item)] if _has_section_key(value) else []
        )
        choices = [item for item in value if item not in comments]
        for choice in choices:
            located, scope = locate(choice)
            combined.extend(item for item in located if item not in combined)
            scopes.append(scope)
        for comment in comments:
            annotation(combined, comment, choices)
        return combined, "section" if all(
            scope == "section" for scope in scopes
        ) else "explicit_range"
    if not isinstance(value, str):
        raise ValueError("unknown_location")
    clue = value.strip()
    if clue.isdigit():
        raise ValueError("ambiguous_numeric_location")
    exact = _literal_nodes(clue, navigation)
    if exact:
        if len(exact) != 1:
            raise ValueError("ambiguous_location")
        return _node_ranges(exact[0], parsed, mode, notes), "section"
    keyed_path = re.fullmatch(
        _PATH_LABEL
        + r"(.+?)\s*[（(]\s*section[ _]key\s*[：:]\s*`?(section:[A-Za-z0-9_-]+)`?\s*[）)]",
        clue,
        re.I,
    )
    if keyed_path:
        selected, scope = locate(keyed_path.group(2))
        annotation(selected, keyed_path.group(1), keyed_path.group(2))
        return selected, scope
    unlabelled = re.sub(
        r"^(?:heading[ _]path|section[ _]key|章节路径|标题路径)\s*[：:]\s*", "", clue, flags=re.I
    )
    if unlabelled != clue:
        return locate(unlabelled)
    parts = _location_choices(re.sub(r"[,，]\s*(?=" + _PATH_LABEL + ")", ";", clue, flags=re.I))
    if len(parts) > 1:
        return locate(parts)
    labelled_path = re.search(r"[（(]\s*" + _PATH_LABEL + r"([^）)]+)[）)]\s*$", clue, re.I)
    if labelled_path:
        primary = clue[: labelled_path.start()].strip(" `")
        if re.fullmatch(r"section:[A-Za-z0-9_-]+", primary):
            selected, scope = locate(primary)
            annotation(selected, labelled_path.group(1), primary)
            return selected, scope
        return locate(labelled_path.group(1))
    keyed_selection = re.fullmatch(r"`?(section:[A-Za-z0-9_-]+)`?\s*[（(](.+)[）)]", clue)
    if keyed_selection:
        return locate([keyed_selection.group(1), keyed_selection.group(2)])
    candidates = []
    for node in navigation:
        path = node.get("heading_path", [])
        labels = {node.get("section_key"), " / ".join(path), " > ".join(path)}
        if path:
            labels.add(path[-1])
        if clue in labels:
            candidates.append(node)
    if not candidates:
        # An exact suffix can omit ancestor headings, but must identify one node.
        path_parts = heading_path(clue)
        if len(path_parts) > 1:
            candidates = [
                node
                for node in navigation
                if node.get("heading_path", [])[-len(path_parts) :] == path_parts
            ]
    if len(candidates) != 1:
        if candidates:
            raise ValueError("ambiguous_location")
        if " / " in clue:
            parts = [part.strip() for part in clue.split(" / ") if part.strip()]
            combined = []
            if len(parts) > 1:
                for part in parts:
                    located, _ = locate(part)
                    combined.extend(value for value in located if value not in combined)
                return combined, "section"
        raise ValueError("unknown_location")
    return _node_ranges(candidates[0], parsed, mode, notes), "section"


def context_choices(value: Any, navigation: list[dict[str, Any]]) -> list[Any]:
    """Isolate optional clues before resolving, without stringifying containers."""
    choices = value if isinstance(value, list) else [value]
    if isinstance(value, str) and not _literal_nodes(value.strip(" `"), navigation):
        choices = [part for choice in _location_choices(value) for part in _key_choices(choice)]
    primary = next((item for item in choices if _has_section_key(item)), None)
    comments = [item for item in choices if _is_path_annotation(item)]
    if primary is not None and comments:
        return [
            [primary, *comments],
            *[item for item in choices if item != primary and item not in comments],
        ]
    return choices


def within_target(ranges: list[RangeValue], target: list[RangeValue], parsed: Any) -> bool:
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


def resolve_hint(
    value: Any,
    navigation: list[dict[str, Any]],
    parsed: Any,
    evidence: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> tuple[list[RangeValue], str]:
    """Map exact positions across the bound source; never invent a fallback range."""
    if isinstance(value, dict) and value.get("format") == "bound-location-v1":
        if value.get("unresolved"):
            raise ValueError("unresolved_bound_location")
        for selected in value["ranges"]:
            range_intervals(selected, parsed, "bound planning hint")
        return value["ranges"], "explicit_range"
    choices = context_choices(value, navigation)
    if isinstance(value, str) and len(choices) > 1:
        ranges: list[RangeValue] = []
        scopes = []
        for choice in choices:
            selected, scope = resolve_hint(choice, navigation, parsed, evidence, notes)
            ranges.extend(item for item in selected if item not in ranges)
            scopes.append(scope)
        return ranges, "section" if all(
            scope == "section" for scope in scopes
        ) else "explicit_range"
    try:
        return resolve_location(
            value, navigation, [], parsed, evidence, mode="context", notes=notes
        )
    except ValueError:
        if not isinstance(value, str):
            raise
        keys = set(re.findall(r"(?<![\w:-])section:[A-Za-z0-9_-]+(?![\w:-])", value))
        if len(keys) != 1:
            raise
        key = next(iter(keys))
        selected, scope = resolve_location(key, navigation, [], parsed, mode="context")
        if notes is not None:
            notes.append("按已知章节键定位；显示文字保留为待核对线索：" + value[:120])
        return selected, scope
