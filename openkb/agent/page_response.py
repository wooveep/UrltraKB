"""Page response validation; invalid JSON must never become an empty page."""

import json
import re

from json_repair import repair_json


class PageResponseError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def page_repair_message(
    code: str,
    detail: str,
    *,
    page_update: bool = False,
    response_protocol: str = "page-json",
    expected_sections: tuple[str, ...] = (),
) -> dict:
    """The same bounded response correction is measured and sent without alteration."""
    return {
        "role": "user",
        "content": (
            f"The page response failed validation ({code}): {detail}. "
            + (
                "Return one updated block-edit object or explicit unchanged result "
                "with a reason, using the captured base_sha256. "
                if page_update
                else (
                    "Return exactly one JSON object with description and sections. "
                    f"Include these ids once each: {expected_sections}. "
                    if response_protocol == "summary-json"
                    else "Return exactly one JSON object with string description "
                    "and nonempty content, retaining the other required fields from the "
                    "original request (including type and source_ranges when requested). "
                )
            )
            + "Do not append a second object or note. Keep supported source facts only. "
            "If truncated, retain all necessary new information in complete edits or statements."
        ),
    }


def parse_summary(text: str, expected_sections: tuple[str, ...]) -> dict:
    """Accept only the declared, complete JSON protocol; never invent missing bytes."""

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_fields)
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
        parsed = json.loads(cleaned)
    except ValueError as error:
        if strict:
            raise PageResponseError("summary_invalid_json", str(error)) from error
        if "Unterminated string" in str(error):
            raise PageResponseError("page_truncated_json", "Unterminated JSON string") from error
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
