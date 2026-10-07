"""Page response validation; invalid JSON must never become an empty page."""

import json
import re

from json_repair import repair_json


class PageResponseError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def parse_page(text: str) -> dict:
    cleaned = text.strip()
    if not cleaned:
        raise PageResponseError("page_empty_response", "No page response was returned")
    if cleaned.startswith("```"):
        cleaned = cleaned.partition("\n")[2].removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError as error:
        if "Unterminated string" in str(error):
            raise PageResponseError("page_truncated_json", "Unterminated JSON string") from error
        parsed = repair_json(cleaned, return_objects=True)
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
