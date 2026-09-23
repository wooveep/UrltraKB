"""Typed diagnostics emitted by the DocumentPlan validators themselves."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, NoReturn


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    category: str
    expected: Any
    actual: Any = None
    source_ranges: list[Any] = field(default_factory=list)
    related_paths: list[str] = field(default_factory=list)
    allowed_action: str = "field_repair"
    line: int | None = None
    column: int | None = None


class PlanValidationError(ValueError):
    """A rejected candidate with trusted, machine-readable field diagnostics."""

    def __init__(self, message: str, issues: list[ValidationIssue]):
        super().__init__(message)
        self.issues = tuple(issues)


def parse_plan_json(raw: str | bytes) -> Any:
    """Parse once while retaining nested duplicate-field locations."""

    class ObjectPairs(list):
        pass

    def convert(value: Any, path: str) -> Any:
        if isinstance(value, ObjectPairs):
            result: dict[str, Any] = {}
            for name, item in value:
                field_path = (
                    (f"{path}.{name}" if name.isidentifier() else f"{path}[{json.dumps(name)}]")
                    if path
                    else name
                )
                converted = convert(item, field_path)
                if name in result:
                    reject(
                        "Duplicate model response field",
                        code="json_duplicate_field",
                        path=field_path,
                        category="syntax",
                        expected="one value per field",
                        actual=name,
                        allowed_action=("syntax_repair" if result[name] == converted else "stop"),
                    )
                result[name] = converted
            return result
        if isinstance(value, list):
            return [convert(item, f"{path}[{index}]") for index, item in enumerate(value)]
        return value

    return convert(json.loads(raw, object_pairs_hook=ObjectPairs), "")


def reject(
    message: str,
    *,
    code: str,
    path: str,
    category: str,
    expected: Any,
    actual: Any = None,
    source_ranges: list[Any] | None = None,
    allowed_action: str = "field_repair",
) -> NoReturn:
    raise PlanValidationError(
        message,
        [
            ValidationIssue(
                code=code,
                path=path,
                category=category,
                expected=expected,
                actual=actual,
                source_ranges=source_ranges or [],
                allowed_action=allowed_action,
            )
        ],
    )
