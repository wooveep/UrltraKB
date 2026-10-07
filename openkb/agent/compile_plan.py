"""Validate model plans before page generation; repair only the plan, at most once."""

from collections.abc import Callable
from dataclasses import dataclass, field

from openkb.compilation_report import CompilationIncomplete, report_compile_issue


@dataclass
class PlanGroup:
    create: list[dict] = field(default_factory=list)
    update: list[dict] = field(default_factory=list)
    related: list[str] = field(default_factory=list)


@dataclass
class CompilePlan:
    concepts: PlanGroup
    entities: PlanGroup
    normalized: bool = False


class CompilePlanError(CompilationIncomplete):
    def __init__(self, detail: str, code: str):
        super().__init__((code,), ("concepts", "entities"), detail)


def normalize_plan(
    parsed: object,
    *,
    existing: dict[str, set[str]],
    entity_types: frozenset[str],
    sanitize: Callable[[str], str],
) -> CompilePlan:
    """Accept explicit actions and unambiguous legacy/new array forms, never discard items."""
    normalized = isinstance(parsed, list)
    actions = {"create", "update", "related"}
    if isinstance(parsed, list):
        parsed = {"concepts": parsed}
    if not isinstance(parsed, dict):
        raise ValueError("plan must contain concepts/entities or legacy action keys")
    # An explicit empty legacy object and the legacy summary/action envelope
    # remain valid; unrelated objects must never silently turn into empty plans.
    if not parsed or set(parsed) <= actions:
        parsed = {"concepts": parsed}
    elif set(parsed) & actions and set(parsed) <= actions | {
        "description",
        "brief",
        "content",
        "entities",
    }:
        parsed = {
            "concepts": {key: value for key, value in parsed.items() if key in actions},
            "entities": parsed.get("entities", {}),
        }
    if set(parsed) - {"concepts", "entities"}:
        raise ValueError("unknown or mixed top-level plan keys")
    groups = {}
    for kind in ("concepts", "entities"):
        raw = parsed.get(kind, {})
        array = isinstance(raw, list)
        if array:
            normalized = True
            raw = {"create": raw}
        if not isinstance(raw, dict) or set(raw) - actions:
            raise ValueError(f"{kind} must contain only create/update/related arrays")
        group = PlanGroup()
        seen: set[str] = set()
        for action in ("create", "update", "related"):
            items = raw.get(action, [])
            if not isinstance(items, list):
                raise ValueError(f"{kind}.{action} must be an array")
            for index, item in enumerate(items):
                location = f"{kind}.{action}[{index}]"
                if action == "related":
                    name = item
                else:
                    if not isinstance(item, dict) or set(item) - {"name", "title", "type", "brief"}:
                        raise ValueError(
                            f"{location} must be a page object with name/title/type/brief"
                        )
                    name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f"{location}.name must be a nonempty string")
                slug = sanitize(name)
                if slug in seen:
                    raise ValueError(f"{location}: duplicate/conflicting action for {slug}")
                seen.add(slug)
                if action in {"update", "related"} and slug not in existing[kind]:
                    raise ValueError(f"{location}: {slug} does not exist; use create")
                if action == "create" and slug in existing[kind]:
                    raise ValueError(
                        f"{location}: {slug} already exists; specify update or related"
                    )
                if action == "related":
                    group.related.append(slug)
                    continue
                title = item.get("title", name)
                if not isinstance(title, str) or not title.strip():
                    raise ValueError(f"{location}.title must be a nonempty string")
                value = {"name": slug, "title": title.strip()}
                if "brief" in item:
                    if not isinstance(item["brief"], str):
                        raise ValueError(f"{location}.brief must be a string")
                    value["brief"] = item["brief"]
                if kind == "entities":
                    entity_type = item.get("type", "other")
                    if not isinstance(entity_type, str) or entity_type not in entity_types:
                        raise ValueError(f"{location}.type must be one of {sorted(entity_types)}")
                    value["type"] = entity_type
                getattr(group, action).append(value)
        groups[kind] = group
    return CompilePlan(groups["concepts"], groups["entities"], normalized)


def request_compile_plan(
    request: Callable[[str | None], str], parse: Callable, **options
) -> CompilePlan:
    correction = None
    for attempt in range(2):
        raw = request(correction)
        parsing = True
        try:
            parsed = parse(raw)
            parsing = False
            plan = normalize_plan(parsed, **options)
        except ValueError as error:
            if attempt:
                code = "concept_plan_unparseable" if parsing else "malformed_plan_items"
                report_compile_issue(code, "concepts", "entities")
                raise CompilePlanError(str(error), code) from error
            correction = (
                "The previous JSON plan failed validation: "
                + str(error)
                + "\nReturn a corrected JSON plan with concepts and entities objects, each with "
                "create/update/related arrays. Use supported types and existing targets. "
                "Preserve valid proposals. Previous output:\n" + raw[:8000]
            )
        else:
            if attempt:
                report_compile_issue("compile_plan_repaired")
            elif plan.normalized:
                report_compile_issue("compile_plan_normalized")
            return plan
    raise AssertionError("Unreachable plan retry state")
