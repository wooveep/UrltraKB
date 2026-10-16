"""One summary contract for prompt examples, local validation and structured delivery.

Prompt rendering intentionally retains the v5 bytes: accepted source windows and
their consumed repair opportunities must remain addressable after an upgrade.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

NonemptyText = Annotated[str, Field(strict=True, pattern=r"\S")]


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Section(_Envelope):
    id: StrictStr = Field(json_schema_extra={"example": "the supplied output section id"})
    content: NonemptyText = Field(json_schema_extra={"example": "complete Markdown knowledge"})


class _Sections(_Envelope):
    description: NonemptyText = Field(json_schema_extra={"example": "brief document description"})
    sections: list[_Section]


class _Part(_Envelope):
    kind: Literal["prose", "excerpt", "derived_template"]
    text: StrictStr
    refs: list[StrictStr]


class _ReferencedSection(_Envelope):
    id: StrictStr
    parts: list[_Part] = Field(min_length=1)


class _ReferencedSummary(_Envelope):
    description: NonemptyText = Field(json_schema_extra={"example": "brief document description"})
    sections: list[_ReferencedSection]


class _Page(_Envelope):
    # Empty legacy page descriptions remain valid; content has always been required.
    description: StrictStr = Field(
        description=(
            "A single sentence (under 100 chars) describing the document's main contribution"
        )
    )
    content: NonemptyText = Field(
        description="The full summary in Markdown. Include key concepts, findings, ideas, "
        "and [[wikilinks]] to concepts that could become cross-document concept pages"
    )


class SummaryViolation(ValueError):
    def __init__(self, code: str, detail: str, fields: tuple[dict, ...]):
        self.code, self.fields = code, fields
        super().__init__(detail)


def _type(value) -> str:
    return {
        type(None): "null",
        bool: "boolean",
        str: "string",
        list: "array",
        dict: "object",
        int: "integer",
        float: "number",
    }.get(type(value), type(value).__name__)


def _path(parts: tuple) -> str:
    return "$" + "".join(
        f"[{part}]"
        if isinstance(part, int)
        else f".{part}"
        if str(part).isidentifier()
        else f"[{json.dumps(part, ensure_ascii=False)}]"
        for part in parts
    )


def _schema(model: type[BaseModel]) -> dict:
    raw = model.model_json_schema()
    definitions = raw.get("$defs", {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(definitions[value["$ref"].split("/")[-1]])
        return {
            key: inline(item)
            for key, item in value.items()
            if key not in {"$defs", "title", "example"}
        }

    return inline(raw)


@dataclass(frozen=True)
class SummaryProtocol:
    name: str
    model: type[BaseModel]

    def schema(
        self, expected_sections: tuple[str, ...] = (), excerpt_ids: tuple[str, ...] = ()
    ) -> dict:
        schema = _schema(self.model)
        if self.name in {"summary-json", "summary-parts-json"} and expected_sections:
            schema["properties"]["sections"]["items"]["properties"]["id"]["enum"] = list(
                expected_sections
            )
        if self.name == "summary-parts-json" and excerpt_ids:
            schema["properties"]["sections"]["items"]["properties"]["parts"]["items"]["properties"][
                "refs"
            ]["items"]["enum"] = list(excerpt_ids)
        return schema

    def example(self) -> str:
        if self.name == "summary-parts-json":
            return json.dumps(
                {
                    "description": "brief document description",
                    "sections": [
                        {
                            "id": "the supplied output section id",
                            "parts": [
                                {
                                    "kind": "prose",
                                    "text": "explain the source and its conditions",
                                    "refs": [],
                                },
                                {
                                    "kind": "excerpt",
                                    "text": "",
                                    "refs": ["a supplied reference id"],
                                },
                            ],
                        }
                    ],
                }
            )
        value = {
            "description": self.model.model_fields["description"].json_schema_extra["example"],
            "sections": [
                {
                    key: field.json_schema_extra["example"]
                    for key, field in _Section.model_fields.items()
                }
            ],
        }
        return json.dumps(value).replace('"sections": [', '"sections":\n [')

    def structure_hint(self) -> str:
        def describe(schema):
            kind = schema["type"]
            if kind == "array":
                return "array of " + describe(schema["items"])
            if kind == "object":
                return "objects with " + ", ".join(
                    f"{key} ({describe(field)})" for key, field in schema["properties"].items()
                )
            return "nonempty string" if kind == "string" and schema.get("pattern") else kind

        return "Use only " + " and ".join(
            f"{key} ({describe(field)})" for key, field in self.schema()["properties"].items()
        )

    def repair_instruction(self, expected_sections: tuple[str, ...]) -> str:
        first, second = self.model.model_fields
        if self.name == "summary-parts-json":
            return (
                "Return description and sections with these IDs exactly once: "
                f"{expected_sections}. "
                "Each section has parts with kind, text and refs. Use supplied refs for excerpts; "
                "Prose permits ordinary Markdown including code examples. "
                "Derived templates require concrete source refs. "
            )
        if self.name == "summary-json":
            return (
                f"Return exactly one JSON object with {first} and {second}. "
                f"Include these ids once each: {expected_sections}. "
            )
        return (
            f'Return exactly one JSON object with only the two keys "{first}" and "{second}", '
            "both strings, with nonempty content. Do not add type or other fields. "
        )

    def validate(self, value, expected_sections: tuple[str, ...] = ()) -> dict:
        try:
            decoded = self.model.model_validate(value).model_dump()
        except ValidationError as error:
            fields = []
            for entry in error.errors(include_url=False, include_context=False):
                parts, kind = tuple(entry["loc"]), entry["type"]
                expected = {
                    "missing": "required field",
                    "extra_forbidden": "no additional field",
                    "string_type": "string",
                    "list_type": "array",
                    "model_type": "object",
                    "string_too_short": "nonempty string",
                    "string_pattern_mismatch": "nonempty string",
                }.get(kind, "declared field type")
                actual = "missing" if kind == "missing" else _type(entry.get("input"))
                field = parts[0] if parts else "root"
                child = len(parts) > 1
                rule = (
                    "section_fields"
                    if child and kind in {"missing", "extra_forbidden", "model_type"}
                    else "section_content"
                    if child and parts[-1] == "content"
                    else "section_ids"
                    if child
                    else "root_fields"
                    if kind in {"missing", "extra_forbidden"}
                    else "sections_array"
                    if field == "sections"
                    else "description"
                    if field == "description"
                    else "page_content"
                    if field == "content"
                    else "root_object"
                )
                if self.name == "summary-parts-json" and child:
                    rule = (
                        "reference_part"
                        if len(parts) > 2 and parts[2] == "parts"
                        else "reference_section"
                    )
                    if parts[-1] == "kind" and kind == "literal_error":
                        expected = "prose, excerpt or derived_template"
                fields.append(
                    {"path": _path(parts), "expected": expected, "actual": actual, "rule": rule}
                )
            if self.name in {"summary-json", "summary-parts-json"}:
                child_error = bool(fields) and all(
                    len(entry["loc"]) > 1 for entry in error.errors()
                )
                code = "summary_invalid_sections" if child_error else "summary_invalid_shape"
                detail = (
                    "Unknown, duplicate or empty section"
                    if child_error
                    else "Expected description and sections"
                )
            else:
                content = next((field for field in fields if field["path"] == "$.content"), None)
                code = (
                    "page_empty_content"
                    if content and content["expected"] == "nonempty string"
                    else "page_wrong_field_type"
                    if content and content["expected"] == "string"
                    else "summary_invalid_shape"
                )
                detail = "Expected description and content"
            details = "; ".join(
                f"{f['path']}: expected {f['expected']}, got {f['actual']}" for f in fields
            )
            raise SummaryViolation(code, f"{detail}; {details}", tuple(fields)) from error
        if self.name not in {"summary-json", "summary-parts-json"}:
            return decoded
        if self.name == "summary-parts-json":
            for i, section in enumerate(decoded["sections"]):
                for j, part in enumerate(section["parts"]):
                    kind, text, refs = part["kind"], part["text"], part["refs"]
                    valid = (
                        bool(text.strip()) and not refs
                        if kind == "prose"
                        else not text and bool(refs)
                        if kind == "excerpt"
                        else bool(text.strip())
                    )
                    if not valid or len(refs) != len(set(refs)):
                        field = {
                            "path": f"$.sections[{i}].parts[{j}]",
                            "expected": "valid text and refs for the part kind",
                            "actual": "invalid part fields",
                            "rule": "reference_part",
                        }
                        raise SummaryViolation(
                            "summary_invalid_sections",
                            "Invalid structured reference part",
                            (field,),
                        )
        seen = set()
        fields = []
        for index, section in enumerate(decoded["sections"]):
            if section["id"] not in expected_sections or section["id"] in seen:
                fields.append(
                    {
                        "path": f"$.sections[{index}].id",
                        "expected": "unique requested section ID",
                        "actual": "unknown or duplicate ID",
                        "rule": "section_ids",
                    }
                )
            seen.add(section["id"])
        if not expected_sections or seen != set(expected_sections):
            fields.append(
                {
                    "path": "$.sections",
                    "expected": "every requested section exactly once",
                    "actual": "incomplete section set",
                    "rule": "section_ids",
                }
            )
        if fields:
            raise SummaryViolation(
                "summary_invalid_sections",
                "Unknown, duplicate or missing section; "
                + "; ".join(f["path"] + ": " + f["actual"] for f in fields),
                tuple(fields),
            )
        sections = {section["id"]: section for section in decoded["sections"]}
        return {**decoded, "sections": [sections[key] for key in expected_sections]}


SECTIONED_SUMMARY = SummaryProtocol("summary-json", _Sections)
PAGE_SUMMARY = SummaryProtocol("summary-page-json", _Page)
REFERENCED_SUMMARY = SummaryProtocol("summary-parts-json", _ReferencedSummary)
SHORT_SUMMARY_INSTRUCTIONS = (
    "Return a JSON object with two keys:\n"
    + "\n".join(f'- "{key}": {field.description}' for key, field in _Page.model_fields.items())
    + "\n\nReturn ONLY valid JSON, no fences.\n"
)


def summary_protocol(name: str) -> SummaryProtocol:
    return {
        "summary-json": SECTIONED_SUMMARY,
        "summary-page-json": PAGE_SUMMARY,
        "summary-parts-json": REFERENCED_SUMMARY,
    }[name]


def summary_response_format(
    protocol: str,
    expected_sections: tuple[str, ...] = (),
    *,
    model: str,
    base_url: str | None = None,
    requested: dict | None = None,
    legacy: bool = False,
    excerpt_ids: tuple[str, ...] = (),
) -> dict:
    """No network probing or guessed aliases; unknown endpoints retain JSON mode."""
    from ultrakb.execution_capacity import _model_catalog
    from ultrakb.summary_delivery import deepseek_summary_schema

    card_name = model.removeprefix("openai/")
    card = _model_catalog()[0].get(model, {}) or _model_catalog()[0].get(card_name, {})
    provider = card.get("litellm_provider")
    host = urlsplit(base_url).hostname if base_url else None
    official = host is None or (provider, host) in {
        ("openai", "api.openai.com"),
        ("deepseek", "api.deepseek.com"),
    }
    supported = (
        bool(requested and requested.get("type") == "json_schema")
        or deepseek_summary_schema(model, base_url)
        or (
            not model.startswith("deepseek/")
            and official
            and card.get("supports_response_schema") is True
        )
    )
    if legacy or not supported:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "summary_json"
            if protocol == "summary-parts-json"
            else protocol.replace("-", "_"),
            "strict": True,
            "schema": summary_protocol(protocol).schema(expected_sections, excerpt_ids),
        },
    }


FIELD_REPAIR_HINTS = {
    "root_object": "At $ return one object, never an array or scalar.",
    "root_fields": (
        "At $ use only the required summary fields; remove extra fields and add missing fields."
    ),
    "description": "At $.description use a nonempty string describing this document.",
    "sections_array": "At $.sections use an array of objects, never a string or mapping.",
    "section_fields": "At $.sections[] use only id and content, both strings.",
    "section_content": "At $.sections[].content use a nonempty Markdown string.",
    "section_ids": "At $.sections[].id use every requested ID exactly once, with no other IDs.",
    "page_content": "At $.content use a nonempty Markdown string.",
}

REFERENCE_REPAIR_HINTS = {
    **FIELD_REPAIR_HINTS,
    "section_fields": "Each section has only id and a nonempty parts array.",
    "section_content": "Use a parts array with prose, excerpt or derived_template objects.",
    "reference_section": "Each section has only a requested id and a nonempty parts array.",
    "reference_part": (
        "Each part has kind, text and refs. Prose needs nonempty Markdown and refs: []; "
        "excerpt needs empty text and nonempty supplied refs; derived_template needs "
        "nonempty text and resolvable refs."
    ),
    "excerpt_unknown_reference": "Select only reference IDs present in the supplied catalog.",
}


def summary_repair_hint(fields: tuple[dict, ...], *, protocol="summary-json") -> str | None:
    # Never interpolate model-controlled keys, values, indices or exception text.
    hints = REFERENCE_REPAIR_HINTS if protocol == "summary-parts-json" else FIELD_REPAIR_HINTS
    return next((hints[f["rule"]] for f in fields if f.get("rule") in hints), None)


def prepare_summary_contract(messages: list[dict]) -> list[dict]:
    """Expose existing validation rules at the shared request assembly boundary.

    The acceptance contract and saved source request signatures stay unchanged;
    planners and senders both include this explicit serialization instruction.
    """
    from ultrakb.agent.compile_contract import legacy_prompts, request_version

    hints = (
        legacy_prompts()["reference_repair_hints"]
        if request_version() == 1
        else REFERENCE_REPAIR_HINTS
    )
    contents = [content for m in messages if isinstance(content := m.get("content"), str)]
    if not any(c.startswith("Original document: ") for c in contents) or not any(
        c.startswith("Structured original reference catalog:\n") for c in contents
    ):
        return messages
    rules = {
        "role": "user",
        "content": (
            "Mandatory structured summary output rules: "
            + hints["reference_part"]
            + " "
            + "Include the refs field in EVERY part, including refs: [] for prose; never omit it. "
            + hints.get("template_unproven", "Keep source conditions with generated explanations.")
        ),
    }
    return messages if rules in messages else [*messages, rules]
