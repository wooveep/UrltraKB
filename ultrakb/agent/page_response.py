"""Page response validation; invalid JSON must never become an empty page."""

import ast
import json
import re

from json_repair import repair_json


class PageResponseError(ValueError):
    def __init__(self, code: str, detail: str, *, field_errors: tuple[dict, ...] = ()):
        self.code = code
        self.field_errors = field_errors
        super().__init__(f"{code}: {detail}")


class DuplicateJSONField(ValueError):
    """A duplicate member has no unambiguous protocol value to repair."""


def load_protocol_json(text: str):
    """Reject duplicate members at every object depth, including escaped keys."""

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateJSONField(f"Duplicate field: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique_fields)


def _reject_ambiguous_repair(text: str) -> None:
    """Check original member tokens before syntax repair can overwrite them.

    This only observes object member names; quoted content is a single token,
    and separate objects keep separate name sets. Normal syntax repair remains
    responsible for interpreting all other bytes.
    """
    frames: list[set[str] | None] = []
    separator = re.compile(r"(?:\s|/\*[\s\S]*?\*/|//[^\r\n]*[\r\n]|\#[^\r\n]*[\r\n])*:")
    tokens = re.finditer(
        r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|//[^\r\n]*|/\*[\s\S]*?\*/|\#[^\r\n]*|[{}\[\]]|[^\s,:{}\[\]"']+""",
        text,
    )
    for match in tokens:
        token = match[0]
        if token.startswith(("//", "/*", "#")):
            continue
        if token in {"{", "["}:
            frames.append(set() if token == "{" else None)
        elif token in {"}", "]"}:
            if frames:
                frames.pop()
        elif frames and frames[-1] is not None and separator.match(text, match.end()):
            if token.startswith('"'):
                try:
                    key = json.loads(token)
                except ValueError:
                    continue
            elif token.startswith("'"):
                try:
                    key = ast.literal_eval(token)
                except (ValueError, SyntaxError):
                    continue
            else:
                key = token
            fields = frames[-1]
            assert fields is not None
            if key in fields:
                raise DuplicateJSONField(f"Duplicate field: {key}")
            fields.add(key)


_SUMMARY_REPAIR_DETAILS = {
    "page_truncated_response": (
        "The previous summary was truncated; complete every required section"
    ),
    "summary_invalid_json": "Return one complete JSON object without fences or trailing text",
    "summary_invalid_shape": "",  # Rendered from the same model as validation and wire schema.
    "summary_invalid_sections": "Section IDs must be complete, unique and exactly as requested",
    "page_excerpt_invalid": "Use only supplied original excerpt IDs, each on its own line",
}

_PAGE_REPAIR_DETAILS = {
    "page_empty_response": "Return one nonempty JSON page object",
    "page_empty_content": "Return nonempty supported content",
    "page_duplicate_field": "Every JSON object member must occur exactly once",
    "page_multiple_objects": "Return exactly one page object",
    "page_wrong_shape": "Return exactly one JSON object with page fields",
    "page_wrong_field_type": "Description, content and type must be strings",
    "page_truncated_json": "Complete every JSON string and object",
    "page_excerpt_invalid": "Select supplied original excerpt IDs in visible top-level paragraphs.",
    "page_invalid_source_ranges": "Select only supplied figure source range ID strings.",
    "page_incomplete_coverage": "Acknowledge every required covered_ranges ID exactly once.",
    "page_truncated_response": "Return complete JSON and complete statements for this batch.",
    "page_invalid_response": "Return a nonempty page with string description, content and type.",
    "page_unchanged_not_allowed": "New pages require nonempty supported content",
}

_UPDATE_REPAIR_DETAILS = {
    "page_update_invalid": "Use the declared update fields, result type and editing targets",
    "page_update_conflict": "Return at most one consistent set of edits for each target",
    "page_manual_content_removed": "Retain every supplied human addition verbatim",
    "page_assembly_invalid": "Keep complete Markdown blocks and their required separators",
    "page_invalid_source_ranges": "Select only the supplied figure source range IDs",
    "page_excerpt_invalid": "Use only supplied excerpt IDs in visible top-level paragraphs",
    "page_empty_content": "Keep a nonempty page body",
    "page_truncated_response": "Complete the required JSON and every necessary edit",
}


def update_repair_messages() -> tuple[dict, ...]:
    """The finite update corrections are shared by admission and actual delivery."""
    return tuple(page_repair_message(code, "", page_update=True) for code in _UPDATE_REPAIR_DETAILS)


def summary_repair_messages(
    expected_sections: tuple[str, ...], *, protocol="summary-json"
) -> tuple[dict, ...]:
    """All possible summary corrections, shared by window admission and delivery."""
    from ultrakb.agent.summary_protocol import FIELD_REPAIR_HINTS, REFERENCE_REPAIR_HINTS

    generic = tuple(
        page_repair_message(
            code, "", response_protocol=protocol, expected_sections=expected_sections
        )
        for code in _SUMMARY_REPAIR_DETAILS
    )
    return generic + tuple(
        page_repair_message(
            "summary_invalid_shape",
            "",
            response_protocol=protocol,
            expected_sections=expected_sections,
            field_errors=({"rule": rule},),
        )
        for rule in (
            REFERENCE_REPAIR_HINTS if protocol == "summary-parts-json" else FIELD_REPAIR_HINTS
        )
    )


def page_repair_message(
    code: str,
    detail: str,
    *,
    page_update: bool = False,
    response_protocol: str = "page-json",
    expected_sections: tuple[str, ...] = (),
    field_errors: tuple[dict, ...] = (),
) -> dict:
    """Keep finite correction rules and a bounded local page-validation reason."""
    from ultrakb.agent.summary_protocol import (
        REFERENCE_REPAIR_HINTS,
        summary_protocol,
        summary_repair_hint,
    )

    if page_update:
        code = code if code in _UPDATE_REPAIR_DETAILS else "page_update_invalid"
        detail = _UPDATE_REPAIR_DETAILS[code]

    if (
        response_protocol in {"summary-json", "summary-page-json", "summary-parts-json"}
        and not page_update
    ):
        if code not in _SUMMARY_REPAIR_DETAILS:
            code = "summary_invalid_shape"
        detail = _SUMMARY_REPAIR_DETAILS[code]
        detail = (
            summary_repair_hint(field_errors, protocol=response_protocol)
            or detail
            or summary_protocol(response_protocol).structure_hint()
        )
    elif not page_update:
        # Keep the local target and specific validation reason, while a field
        # name, response body or complete ID set remains in the local receipt.
        rule = _PAGE_REPAIR_DETAILS.get(code, _PAGE_REPAIR_DETAILS["page_invalid_response"])
        if detail and detail != rule:
            prefix = " ".join(detail[:256].split())
            detail = f"{rule}; local validation: {prefix}" + ("…" if len(detail) > 256 else "")
        else:
            detail = rule
    return {
        "role": "user",
        "content": (
            f"The page response failed validation ({code}): {detail}. "
            + (
                "Return one updated block-edit object or explicit unchanged result "
                "with a reason, using the captured base_sha256. Retain all required fields "
                "from the original request, including type and source_ranges when requested. "
                "Use only supplied source range IDs and include every range that supports "
                "the returned edits. Edit only blocks that need a supported change; omit "
                "unchanged blocks instead of rewriting the entire page. "
                if page_update
                else (
                    summary_protocol(response_protocol).repair_instruction(expected_sections)
                    if response_protocol
                    in {"summary-json", "summary-page-json", "summary-parts-json"}
                    else "Return exactly one JSON object with string description "
                    "and nonempty content, retaining the other required fields from the "
                    "original request (including type and source_ranges when requested). "
                )
            )
            + "If using an original excerpt catalog reference, put each [[original:ID]] "
            "alone on its own line, without prose or a bullet marker. "
            + (
                REFERENCE_REPAIR_HINTS["reference_part"] + " "
                if response_protocol == "summary-parts-json"
                else ""
            )
            + "Do not append a second object or note. Keep supported source facts only. "
            "If truncated, retain all necessary new information in complete edits or statements."
        ),
    }


def parse_summary(
    text: str, expected_sections: tuple[str, ...], *, protocol: str | None = None
) -> dict:
    """Accept only the declared, complete JSON protocol; never invent missing bytes."""
    try:
        value = load_protocol_json(text)
    except ValueError as error:
        raise PageResponseError("summary_invalid_json", str(error)) from error
    if protocol is None:
        referenced = (
            isinstance(value, dict)
            and isinstance(value.get("sections"), list)
            and any(
                isinstance(section, dict) and "parts" in section for section in value["sections"]
            )
        )
        protocol = "summary-parts-json" if referenced else "summary-json"
    return _validate_summary(value, protocol, expected_sections)


def _validate_summary(value, protocol: str, expected_sections: tuple[str, ...] = ()) -> dict:
    from ultrakb.agent.summary_protocol import SummaryViolation, summary_protocol

    try:
        return summary_protocol(protocol).validate(value, expected_sections)
    except SummaryViolation as error:
        raise PageResponseError(error.code, str(error), field_errors=error.fields) from error


def parse_page(text: str, *, strict: bool = False) -> dict:
    cleaned = text.strip()
    if not cleaned:
        raise PageResponseError("page_empty_response", "No page response was returned")
    if cleaned.startswith("```"):
        if strict:
            raise PageResponseError("summary_invalid_json", "Return JSON without code fences")
        cleaned = cleaned.partition("\n")[2].removesuffix("```").strip()
    try:
        parsed = load_protocol_json(cleaned)
    except DuplicateJSONField as error:
        code = "summary_invalid_json" if strict else "page_duplicate_field"
        raise PageResponseError(code, str(error)) from error
    except ValueError as error:
        if strict:
            raise PageResponseError("summary_invalid_json", str(error)) from error
        if "Unterminated string" in str(error):
            raise PageResponseError("page_truncated_json", "Unterminated JSON string") from error
        try:
            _reject_ambiguous_repair(cleaned)
        except DuplicateJSONField as duplicate:
            raise PageResponseError("page_duplicate_field", str(duplicate)) from duplicate
        parsed = repair_json(cleaned, return_objects=True)
    if strict:
        return _validate_summary(parsed, "summary-page-json")
    if isinstance(parsed, list):
        if len(parsed) > 1:
            raise PageResponseError(
                "page_multiple_objects", "Expected one page object, got multiple objects"
            )
        if len(parsed) != 1 or not isinstance(parsed[0], dict):
            raise PageResponseError("page_wrong_shape", "Expected one JSON page object")
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        raise PageResponseError("page_wrong_shape", "Expected one JSON page object")
    for key in ("description", "content", "type"):
        if key in parsed and not isinstance(parsed[key], str):
            raise PageResponseError("page_wrong_field_type", f"{key} must be a string")
    if not parsed.get("content", "").strip():
        raise PageResponseError("page_empty_content", "The page content is empty")
    return parsed


def page_fields(raw: str) -> tuple[str, str, dict | None]:
    # Keep the existing plain-Markdown compatibility only for non-JSON output.
    scalar = raw.strip() in {"null", "true", "false"} or re.fullmatch(
        r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|".*"', raw.strip(), re.S
    )
    if raw.strip() and not scalar and not raw.lstrip().startswith(("{", "[", "```json")):
        if not raw.lstrip().startswith("```"):
            return "", raw, None
    obj = parse_page(raw)
    return obj.get("description", ""), obj["content"], obj
