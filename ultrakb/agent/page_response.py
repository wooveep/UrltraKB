"""Page response validation; invalid JSON must never become an empty page."""

import ast
import json
import re

from json_repair import repair_json


class PageResponseError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
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
    "summary_invalid_shape": "Use exactly description and sections, with nonempty string content",
    "summary_invalid_sections": "Section IDs must be complete, unique and exactly as requested",
    "page_excerpt_invalid": "Use only supplied original excerpt IDs, each on its own line",
    "page_literal_conflict": "Code or table syntax must match a complete supplied original excerpt",
    "page_numeric_conflict": "Remove numbers unsupported by the supplied original sections",
}


def summary_repair_messages(expected_sections: tuple[str, ...]) -> tuple[dict, ...]:
    """All possible summary corrections, shared by window admission and delivery."""
    return tuple(
        page_repair_message(
            code, "", response_protocol="summary-json", expected_sections=expected_sections
        )
        for code in _SUMMARY_REPAIR_DETAILS
    )


def page_repair_message(
    code: str,
    detail: str,
    *,
    page_update: bool = False,
    response_protocol: str = "page-json",
    expected_sections: tuple[str, ...] = (),
) -> dict:
    """Summary corrections have finite shapes; raw diagnostics are not model input."""
    if response_protocol == "summary-json" and not page_update:
        if code not in _SUMMARY_REPAIR_DETAILS:
            code = "summary_invalid_shape"
        detail = _SUMMARY_REPAIR_DETAILS[code]
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
                    "Return exactly one JSON object with description and sections. "
                    f"Include these ids once each: {expected_sections}. "
                    if response_protocol == "summary-json"
                    else "Return exactly one JSON object with only the two keys "
                    '"description" and "content", '
                    "both strings, with nonempty content. Do not add type or other fields. "
                    if response_protocol == "summary-page-json"
                    else "Return exactly one JSON object with string description "
                    "and nonempty content, retaining the other required fields from the "
                    "original request (including type and source_ranges when requested). "
                )
            )
            + (
                "Keep standalone paths from prose or qualified complete native cells in inline "
                "code with their exact original spelling. Do not put a path or decoded table "
                "cell value in a new fenced code block, even when also including an original "
                "excerpt reference. For code or table syntax, use the complete original excerpt "
                "catalog ID instead of rewriting it. Original HTML table excerpts must retain "
                "all tags and entity encoding; qualified complete cell values support exact "
                "inline paths, not new fenced excerpts. "
                if code == "page_literal_conflict"
                else ""
            )
            + "If using an original excerpt catalog reference, put each [[original:ID]] "
            "alone on its own line, without prose or a bullet marker. "
            + "Do not append a second object or note. Keep supported source facts only. "
            "If truncated, retain all necessary new information in complete edits or statements."
        ),
    }


def parse_summary(text: str, expected_sections: tuple[str, ...]) -> dict:
    """Accept only the declared, complete JSON protocol; never invent missing bytes."""
    try:
        value = load_protocol_json(text)
    except ValueError as error:
        raise PageResponseError("summary_invalid_json", str(error)) from error
    if (
        not isinstance(value, dict)
        or set(value) != {"description", "sections"}
        or not isinstance(value["description"], str)
        or not value["description"].strip()
        or not isinstance(value["sections"], list)
    ):
        raise PageResponseError("summary_invalid_shape", "Expected description and sections")
    sections = {}
    for section in value["sections"]:
        if (
            not isinstance(section, dict)
            or set(section) != {"id", "content"}
            or not isinstance(section["id"], str)
            or section["id"] not in expected_sections
            or section["id"] in sections
            or not isinstance(section["content"], str)
            or not section["content"].strip()
        ):
            raise PageResponseError(
                "summary_invalid_sections", "Unknown, duplicate or empty section"
            )
        sections[section["id"]] = section
    if not expected_sections or set(sections) != set(expected_sections):
        raise PageResponseError(
            "summary_invalid_sections", "Expected original sections are missing"
        )
    return {**value, "sections": [sections[key] for key in expected_sections]}


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
    if strict and (
        not isinstance(parsed, dict)
        or set(parsed) != {"description", "content"}
        or not isinstance(parsed.get("description"), str)
    ):
        raise PageResponseError("summary_invalid_shape", "Expected description and content")
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
