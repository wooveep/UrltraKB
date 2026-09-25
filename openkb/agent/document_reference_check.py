"""Pure reference candidates, response validation, and restricted plan additions."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from openkb.agent.document_plan import range_intervals
from openkb.agent.document_plan_issues import ValidationIssue, parse_plan_json
from openkb.agent.document_plan_selections import SelectionError, SelectionResolver
from openkb.agent.document_range_validation import interval_is_covered, merged_intervals
from openkb.agent.model_json import json_text
from openkb.sources import content_id

PROTOCOL = "document-reference-check-v3"
V2_PROTOCOL = "document-reference-check-v2"
LEGACY_PROTOCOL = "document-reference-check-v1"
RULE_VERSION = "explicit-reference-v1"
_MARKED = re.compile(
    r"(?:先|需|须|必须|应|请|可)?\s*(?:按|按照|依照|执行|参见|参考|详见|见|遵循|阅读)\s*"
    r"(?:本文档的?|本手册的?)?\s*"
    r"(?P<target>《[^》]{2,80}》|[“\"'「][^”\"'」]{2,80}[”\"'」]|"
    r"第[一二三四五六七八九十百千万0-9.、]+(?:节|章|步)|"
    r"[A-Za-z][A-Za-z0-9 _-]{2,60}(?:手册|文档))"
)
_EXTERNAL = re.compile(
    r"(?:参见|参考|详见|见|按|按照)\s*"
    r"(?P<target>[^\s，。；：:【】]{2,30}手册[“\"'「][^”\"'」]{2,90}[”\"'」])"
)
_ENGLISH = re.compile(
    r"\b(?:see|follow|as described in)\s+(?P<target>section\s+[\d.]+|[A-Z][\w -]{2,60})", re.I
)
_DOCUMENT_CUE = re.compile(
    r"(?:手册|指南|文档|规范|资料|\bmanual\b|\bguide\b|\bhandbook\b|\bdocument\b)", re.I
)


@dataclass(frozen=True)
class ReferenceCandidate:
    reference_key: str
    basis_ranges: tuple[dict[str, Any], ...]
    target_text: str
    target_options: tuple[dict[str, Any], ...]
    availability: str
    affected_page_refs: tuple[str, ...]
    signal_origin: str = "explicit_text"

    def wire(self) -> dict[str, Any]:
        return {
            "reference_key": self.reference_key,
            "basis_ranges": list(self.basis_ranges),
            "target_text": self.target_text,
            "target_options": list(self.target_options),
            "availability": self.availability,
            "affected_page_refs": list(self.affected_page_refs),
            "signal_origin": self.signal_origin,
        }


def _intervals(values: list[Any], parsed: Any) -> dict[int, list[tuple[int, int]]]:
    result: dict[int, list[tuple[int, int]]] = {}
    for value in values:
        for index, start, end in range_intervals(value, parsed, "reference route"):
            result.setdefault(index, []).append((start, end))
    return {index: merged_intervals(rows) for index, rows in result.items()}


def detect_references(
    evidence: dict[str, Any],
    navigation: dict[str, Any] | None,
    delta: dict[str, Any],
    parsed: Any,
) -> list[ReferenceCandidate]:
    """Find explicit source cues; route by exact source membership, not title."""
    nodes = navigation.get("nodes", []) if isinstance(navigation, dict) else []
    nodes = [
        n
        for n in nodes
        if isinstance(n, dict)
        and type(n.get("start")) is int
        and type(n.get("end")) is int
        and 0 <= n["start"] < n["end"] <= len(parsed.blocks)
    ]
    supplied: dict[int, list[tuple[int, int]]] = {}
    for row in evidence.get("blocks", []):
        order, body = row.get("order"), row.get("text")
        if type(order) is not int or not isinstance(body, str):
            continue
        reference = row.get("reference") or {}
        left = reference.get("start", 0) if isinstance(reference, dict) else 0
        right = reference.get("end", len(body)) if isinstance(reference, dict) else len(body)
        supplied.setdefault(order, []).append((left, right))
    supplied = {index: merged_intervals(rows) for index, rows in supplied.items()}
    result: list[ReferenceCandidate] = []
    for block in evidence.get("blocks", []):
        order, identity, body = block.get("order"), block.get("id"), block.get("text")
        if type(order) is not int or not isinstance(identity, str) or not isinstance(body, str):
            continue
        ref = block.get("reference") or {}
        offset = ref.get("start", 0) if isinstance(ref, dict) else 0
        if type(offset) is not int:
            continue
        for match in sorted(
            [*_MARKED.finditer(body), *_EXTERNAL.finditer(body), *_ENGLISH.finditer(body)],
            key=lambda item: item.start(),
        ):
            target = match.group("target").strip("《》“”\"'「」 ")
            basis_start, basis_end = offset + match.start(), offset + match.end()
            basis = {
                "block": identity,
                "start_char": basis_start,
                "end_char": basis_end,
            }
            options = [
                n
                for n in nodes
                if isinstance(n.get("title"), str)
                and n["title"]
                and (target == n["title"] or target in n["title"] or n["title"] in target)
                and n.get("end", n["start"]) > n["start"]
            ]
            # A parent node containing the same text is a location candidate,
            # not a reason to silently choose the first descendant.
            page_refs = tuple(
                page["local_key"]
                for page in delta.get("page_changes", [])
                if isinstance(page, dict)
                and isinstance(page.get("local_key"), str)
                and interval_is_covered(
                    order,
                    basis_start,
                    basis_end,
                    _intervals(page.get("subject_ranges", []), parsed),
                )
            )
            available = "external_not_supplied"
            if len(options) > 1:
                available = "ambiguous"
            elif options:
                available = (
                    "in_window"
                    if all(
                        interval_is_covered(i, 0, parsed.blocks[i].chars, supplied)
                        for i in range(options[0]["start"], options[0]["end"])
                    )
                    else "outside_window"
                )
            elif "本文档" in match.group(0) or re.search(
                r"第[一二三四五六七八九十百千万0-9.、]+[章节步]", target
            ):
                available = "ambiguous"
            key = content_id([RULE_VERSION, evidence.get("parse_id"), identity, basis, target])
            result.append(
                ReferenceCandidate(
                    key,
                    (basis,),
                    target,
                    tuple(
                        {key: n.get(key) for key in ("id", "title", "parent", "start", "end")}
                        for n in options
                    ),
                    available,
                    page_refs,
                    "external_document"
                    if available == "external_not_supplied"
                    and _DOCUMENT_CUE.search(target)
                    and "本文档" not in match.group(0)
                    and "本手册" not in match.group(0)
                    else "explicit_text",
                )
            )
    return result


def target_pairs(
    candidates: list[ReferenceCandidate], delta: dict[str, Any], parsed: Any
) -> list[tuple[str, str]]:
    """Skip only a route whose complete target is already readable on that page."""
    pages = {row["local_key"]: row for row in delta.get("page_changes", [])}
    pending: list[tuple[str, str]] = []
    for candidate in candidates:
        for page_ref in candidate.affected_page_refs:
            page = pages[page_ref]
            values = list(page.get("subject_ranges", []))
            for context in page.get("necessary_context", []):
                values.extend(context.get("ranges", []))
            destinations = _intervals(values, parsed)
            target = candidate.target_options
            if len(target) == 1 and all(
                interval_is_covered(index, 0, parsed.blocks[index].chars, destinations)
                for index in range(target[0]["start"], target[0]["end"])
            ):
                continue
            if (
                any(
                    row.get("missing_target") == candidate.target_text
                    and page.get("target_key") in row.get("affected_pages", [])
                    and any(
                        interval_is_covered(
                            parsed_index,
                            candidate.basis_ranges[0]["start_char"],
                            candidate.basis_ranges[0]["end_char"],
                            _intervals(row.get("location", []), parsed),
                        )
                        for parsed_index, block in enumerate(parsed.blocks)
                        if block.id == candidate.basis_ranges[0]["block"]
                    )
                    for row in delta.get("unresolved", [])
                )
                and candidate.availability == "external_not_supplied"
            ):
                continue
            pending.append((candidate.reference_key, page_ref))
    return pending


@dataclass(frozen=True)
class DecisionResult:
    valid: dict[tuple[str, str], dict[str, Any]]
    issues: tuple[ValidationIssue, ...]
    normalized: tuple[str, ...] = ()


def valid_saved_decisions(
    saved: dict[str, Any], *, pairs: list[tuple[str, str]],
    resolver: SelectionResolver, candidates: list[ReferenceCandidate],
    candidate_hash: str, check_input_hash: str, recovery_key: str,
    max_attempts: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Reject malformed or out-of-scope persisted reference-check state."""
    attempt, status = saved.get("attempt"), saved.get("status")
    if (
        type(attempt) is not int or not 0 <= attempt <= max_attempts
        or status is not None and not isinstance(status, str)
        or status not in {None, "response_received", "response_empty",
                          "response_rejected", "response_validated", "validated",
                          "truncated", "execution_unknown"}
    ):
        raise ValueError("Invalid reference-check recovery state")
    if status == "response_received" and (
        saved.get("response_representation") != "wire"
        or not isinstance(saved.get("response"), str)
        or saved.get("raw_content") is not None
        and not isinstance(saved["raw_content"], str)
        or saved.get("finish_reason") is not None
        and not isinstance(saved["finish_reason"], str)
        or saved.get("response_output_tokens") is not None
        and (type(saved["response_output_tokens"]) is not int
             or saved["response_output_tokens"] < 0)
    ):
        raise ValueError("Invalid recovered reference response")
    rows = saved.get("valid", [])
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("reference_key"), str)
        or not isinstance(row.get("page_ref"), str)
        for row in rows
    ):
        raise ValueError("Invalid recovered reference decisions")
    saved_pairs = [(row["reference_key"], row["page_ref"]) for row in rows]
    if len(set(saved_pairs)) != len(saved_pairs) or not set(saved_pairs) <= set(pairs):
        raise ValueError("Recovered reference decision is outside the request")
    if rows and validate_reference_decisions(
        {"check_protocol": PROTOCOL, "decisions": rows},
        candidate_hash=candidate_hash, check_input_hash=check_input_hash,
        pairs=saved_pairs, resolver=resolver, candidates=candidates,
        required_protocol=PROTOCOL,
    ).issues:
        raise ValueError("Invalid recovered reference decision")
    if status == "validated":
        receipt = saved.get("receipt")
        if (
            not isinstance(saved.get("candidate"), dict)
            or not isinstance(receipt, dict)
            or receipt.get("protocol") != PROTOCOL
            or receipt.get("status") != "accounted"
            or receipt.get("candidate_count") != len(candidates)
            or receipt.get("check_input_hash") != check_input_hash
            or receipt.get("candidate_hash") != candidate_hash
            or receipt.get("receipt_key") != recovery_key
            or receipt.get("attempts") != attempt
            or receipt.get("decisions") != rows
        ):
            raise ValueError("Invalid validated reference-check receipt")
    return dict(zip(saved_pairs, rows, strict=True))


def _issue(code: str, path: str, actual: Any) -> ValidationIssue:
    return ValidationIssue(code, path, "reference", "valid supplied decision", actual)


def validate_reference_decisions(
    raw: Any,
    *,
    candidate_hash: str,
    check_input_hash: str,
    pairs: list[tuple[str, str]],
    resolver: SelectionResolver,
    candidates: list[ReferenceCandidate],
    required_protocol: str | None = None,
    accepted_decisions: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> DecisionResult:
    """Validate choices; v2 response identity is bound by its request receipt."""
    normalized: list[str] = []
    try:
        if isinstance(raw, (str, bytes)):
            cleaned = json_text(raw)
            if cleaned != raw:
                normalized.append("outer_json_wrapper")
            raw = parse_plan_json(cleaned)
    except (TypeError, ValueError) as exc:
        return DecisionResult({}, (_issue("reference_check_invalid", "$", str(exc)),))
    if not isinstance(raw, dict):
        return DecisionResult({}, (_issue("reference_check_invalid", "$", raw),))
    protocol = raw.get("check_protocol")
    if required_protocol is not None and protocol != required_protocol:
        return DecisionResult({}, (_issue("reference_input_mismatch", "$", raw),))
    if protocol in {PROTOCOL, V2_PROTOCOL}:
        if set(raw) != {"check_protocol", "decisions"}:
            return DecisionResult({}, (_issue("reference_check_invalid", "$", raw),))
    elif protocol == LEGACY_PROTOCOL:
        if set(raw) != {"check_protocol", "candidate_hash", "check_input_hash", "decisions"}:
            return DecisionResult({}, (_issue("reference_check_invalid", "$", raw),))
        if raw["candidate_hash"] != candidate_hash or raw["check_input_hash"] != check_input_hash:
            return DecisionResult({}, (_issue("reference_input_mismatch", "$", raw),))
    else:
        return DecisionResult({}, (_issue("reference_input_mismatch", "$", raw),))
    if not isinstance(raw["decisions"], list):
        return DecisionResult(
            {}, (_issue("reference_check_invalid", "decisions", raw["decisions"]),)
        )
    allowed = set(pairs)
    accepted_decisions = accepted_decisions or {}
    by_key = {row.reference_key: row for row in candidates}
    valid: dict[tuple[str, str], dict[str, Any]] = {}
    conflicted: set[tuple[str, str]] = set()
    issues: list[ValidationIssue] = []
    for index, item in enumerate(raw["decisions"]):
        path = f"decisions[{index}]"
        if not isinstance(item, dict):
            issues.append(_issue("reference_check_invalid", path, item))
            continue
        pair = (item.get("reference_key"), item.get("page_ref"))
        if not isinstance(pair[0], str) or pair[0] not in by_key:
            issues.append(_issue("unknown_reference_key", path, item))
            continue
        if not isinstance(pair[1], str) or (pair not in allowed and pair not in accepted_decisions):
            issues.append(_issue("reference_page_out_of_scope", path, item))
            continue
        pair = (pair[0], pair[1])
        decision = item.get("decision")
        expected = {"reference_key", "page_ref", "decision", "reason"}
        if decision == "required_internal":
            expected.add("target_ranges")
        elif decision in {"informational", "uncertain"}:
            expected.add("decision_basis_ranges")
        elif decision != "required_unavailable":
            issues.append(_issue("reference_check_invalid", f"{path}.decision", decision))
            continue
        if (
            set(item) != expected
            or not isinstance(item.get("reason"), str)
            or not item["reason"].strip()
        ):
            issues.append(_issue("reference_check_invalid", path, item))
            continue
        if decision == "required_internal" and not by_key[pair[0]].target_options:
            issues.append(_issue("reference_target_outside_evidence", path, item))
            continue
        if decision == "required_unavailable" and len(by_key[pair[0]].target_options) == 1:
            node = by_key[pair[0]].target_options[0]
            if all(index in resolver.by_order for index in range(node["start"], node["end"])):
                issues.append(_issue("reference_check_invalid", path, item))
                continue
        field = "target_ranges" if decision == "required_internal" else "decision_basis_ranges"
        if field in item:
            try:
                ranges = resolver.decode_ranges(item[field], f"{path}.{field}", target_only=False)
                if not ranges:
                    raise SelectionError("invalid_selection_shape", f"{path}.{field}", item[field])
            except SelectionError as exc:
                issues.append(_issue("reference_target_outside_evidence", exc.path, exc.actual))
                continue
            if decision in {"informational", "uncertain"}:
                selected = _selected_intervals(ranges, resolver.chars)
                if not all(
                    (row := resolver.by_id.get(span["block"])) is not None
                    and interval_is_covered(
                        row["order"], span["start_char"], span["end_char"], selected
                    )
                    for span in by_key[pair[0]].basis_ranges
                ):
                    issues.append(
                        _issue("reference_target_outside_evidence", f"{path}.{field}", item[field])
                    )
                    continue
            if decision == "required_internal" and by_key[pair[0]].target_options:
                allowed_indices = {
                    i
                    for node in by_key[pair[0]].target_options
                    for i in range(node["start"], node["end"])
                }
                if not _range_indices(ranges) <= allowed_indices:
                    issues.append(
                        _issue("reference_target_outside_evidence", f"{path}.{field}", item[field])
                    )
                    continue
        if pair in accepted_decisions:
            if item == accepted_decisions[pair]:
                normalized.append("replayed_accepted_decision")
            else:
                issues.append(_issue("reference_decision_conflict", path, item))
            continue
        if pair in conflicted:
            issues.append(_issue("reference_decision_conflict", path, item))
            continue
        if pair in valid:
            if valid[pair] == item:
                normalized.append("duplicate_identical_decision")
            else:
                conflicted.add(pair)
                valid.pop(pair)
                issues.append(_issue("reference_decision_conflict", path, item))
            continue
        valid[pair] = item
    for pair in pairs:
        if pair not in valid:
            issues.append(
                _issue("reference_decision_missing", f"decisions[{pair[0]}:{pair[1]}]", pair)
            )
    return DecisionResult(valid, tuple(issues), tuple(normalized))


def _range_indices(values: list[Any]) -> set[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, dict):
            result.add(value["block_index"])
        else:
            result.update(range(value[0], value[1]))
    return result


def _selected_intervals(values: list[Any], chars: list[int]) -> dict[int, list[tuple[int, int]]]:
    selected: dict[int, list[tuple[int, int]]] = {}
    for value in values:
        if isinstance(value, dict):
            selected.setdefault(value["block_index"], []).append(
                (value["start_char"], value["end_char"])
            )
        else:
            for index in range(value[0], value[1]):
                selected.setdefault(index, []).append((0, chars[index]))
    return {index: merged_intervals(rows) for index, rows in selected.items()}


def apply_reference_decisions(
    candidate: dict[str, Any],
    decisions: dict[tuple[str, str], dict[str, Any]],
    candidates: list[ReferenceCandidate],
    *,
    resolver: SelectionResolver | None = None,
) -> dict[str, Any]:
    """Modify only relation fields on a copy; caller must recompile all of it."""
    updated = deepcopy(candidate)
    references = {row.reference_key: row for row in candidates}
    pages = {row["local_key"]: row for row in updated["page_changes"]}
    for (reference_key, page_ref), item in decisions.items():
        reference = references[reference_key]
        page = pages[page_ref]
        kind = item["decision"]
        external_v5 = (
            resolver is not None
            and resolver.plan_protocol == "document-plan-v5"
            and reference.availability == "external_not_supplied"
        )
        if kind in {"required_internal", "informational"}:
            _clear_verified_cross_reference(updated, page, reference, resolver)
        if external_v5:
            records = updated.setdefault("external_references", [])
            record = {
                "location": list(reference.basis_ranges),
                "target_document": reference.target_text,
                "target_section": None,
                "affected_pages": [page_ref],
            }
            if not any(
                existing.get("location") == record["location"]
                and page_ref in existing.get("affected_pages", [])
                for existing in records
            ):
                records.append(record)
            if kind in {"required_unavailable", "uncertain"}:
                limitations = page.setdefault("limitations", [])
                limitation = {
                    "ranges": list(reference.basis_ranges),
                    "reason": item["reason"],
                }
                if limitation not in limitations:
                    limitations.append(limitation)
            continue
        if kind == "required_internal":
            addition = {
                "relation": "explicit_reference",
                "ranges": item["target_ranges"],
                "basis_ranges": list(reference.basis_ranges),
                "rationale": item["reason"],
            }
            if not any(
                existing.get("relation") == addition["relation"]
                and (
                    (
                        _selection_covers(existing.get("ranges", []), addition["ranges"], resolver)
                        and _selection_covers(
                            existing.get("basis_ranges", []), addition["basis_ranges"], resolver
                        )
                    )
                    if resolver is not None
                    else existing.get("ranges") == addition["ranges"]
                    and existing.get("basis_ranges") == addition["basis_ranges"]
                )
                for existing in page["necessary_context"]
            ):
                page["necessary_context"].append(addition)
        elif kind in {"required_unavailable", "uncertain"}:
            addition = {
                "location": list(reference.basis_ranges),
                "problem_type": (
                    "missing_external_material"
                    if kind == "required_unavailable"
                    and reference.availability == "external_not_supplied"
                    else "unresolved_cross_reference"
                ),
                "missing_target": reference.target_text,
                "affected_pages": [page_ref],
                "reason": (
                    item["reason"]
                    if kind == "required_unavailable"
                    else f"关系或目标待确认：{item['reason']}"
                ),
            }
            if not any(
                (
                    _selection_covers(existing.get("location", []), addition["location"], resolver)
                    if resolver is not None
                    else existing.get("location") == addition["location"]
                )
                and page_ref in existing.get("affected_pages", [])
                and _same_missing_target(existing.get("missing_target"), addition["missing_target"])
                for existing in updated["unresolved"]
            ):
                updated["unresolved"].append(addition)
    return updated


def _clear_verified_cross_reference(
    candidate: dict[str, Any], page: dict[str, Any], reference: ReferenceCandidate,
    resolver: SelectionResolver | None,
) -> None:
    """Clear only the exact occurrence and target checked for this page."""
    page_keys = {page["local_key"], page.get("target_key")}
    remaining = []
    for row in candidate["unresolved"]:
        same_basis = (
            _same_reference_basis(row.get("location", []), reference.basis_ranges, resolver)
            if resolver is not None else row.get("location") == list(reference.basis_ranges)
        )
        if (
            row.get("problem_type") != "unresolved_cross_reference"
            or not page_keys.intersection(row.get("affected_pages", []))
            or not _exact_missing_target(row.get("missing_target"), reference.target_text)
            or not same_basis
        ):
            remaining.append(row)
            continue
        other_pages = [key for key in row["affected_pages"] if key not in page_keys]
        if other_pages:
            row["affected_pages"] = other_pages
            remaining.append(row)
    candidate["unresolved"] = remaining


def _same_reference_basis(
    location: Any, basis: tuple[dict[str, Any], ...], resolver: SelectionResolver,
) -> bool:
    try:
        selected = _selected_intervals(
            resolver.decode_ranges(location, "unresolved.location", target_only=False),
            resolver.chars,
        )
        basis_rows: list[Any] = []
        for row in basis:
            basis_rows.extend(
                [row] if "block_index" in row
                else resolver.decode_ranges([row], "reference.basis", target_only=False)
            )
        required = _selected_intervals(basis_rows, resolver.chars)
    except (SelectionError, KeyError, TypeError, ValueError):
        return False
    return selected == required


def _exact_missing_target(existing: Any, desired: str) -> bool:
    return (
        isinstance(existing, str)
        and re.sub(r"\W+", "", existing) == re.sub(r"\W+", "", desired)
    )


def _selection_covers(existing: Any, desired: Any, resolver: SelectionResolver) -> bool:
    try:
        old = _selected_intervals(
            resolver.decode_ranges(existing, "existing", target_only=False), resolver.chars
        )
        new = _selected_intervals(
            resolver.decode_ranges(desired, "desired", target_only=False), resolver.chars
        )
    except SelectionError:
        return False
    return all(
        interval_is_covered(index, start, end, old)
        for index, intervals in new.items()
        for start, end in intervals
    )


def _same_missing_target(existing: Any, desired: str) -> bool:
    if not isinstance(existing, str):
        return False
    left = re.sub(r"\W+", "", existing)
    right = re.sub(r"\W+", "", desired)
    return left == right or len(right) >= 4 and right in left
