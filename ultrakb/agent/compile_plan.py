"""Validate model plans before page generation; repair only the plan, at most once."""

from collections.abc import Callable
from dataclasses import dataclass, field

from ultrakb.compilation_report import CompilationIncomplete, report_compile_issue


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
        self.code, self.detail = code, detail
        super().__init__((code,), ("concepts", "entities"), detail)


def normalize_plan(
    parsed: object,
    *,
    existing: dict[str, set[str]],
    entity_types: frozenset[str],
    sanitize: Callable[[str], str],
    require_evidence: bool = False,
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
                    if not isinstance(item, dict) or set(item) - {
                        "name",
                        "title",
                        "type",
                        "brief",
                        "evidence_units",
                        "reading_locations",
                        "aliases",
                    }:
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
                value: dict = {"name": slug, "title": title.strip()}
                if "aliases" in item:
                    from ultrakb.frontmatter import alias_values

                    value["aliases"] = alias_values(item["aliases"])
                if "reading_locations" in item:
                    from ultrakb.agent.source_windows import validate_reading_locations

                    value["reading_locations"] = validate_reading_locations(
                        item["reading_locations"]
                    )
                if require_evidence and "evidence_units" not in item:
                    raise ValueError(f"{location} requires evidence_units from the original source")
                if "evidence_units" in item:
                    from ultrakb.agent.compile_evidence import MAX_EVIDENCE_UNITS

                    units = item["evidence_units"]
                    if (
                        not isinstance(units, list)
                        or not 1 <= len(units) <= MAX_EVIDENCE_UNITS
                        or any(type(n) is not int or n < 1 for n in units)
                        or len(set(units)) != len(units)
                    ):
                        raise ValueError(
                            f"{location}.evidence_units requires 1-6 unique positive ordinals"
                        )
                    value["evidence_units"] = units
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


def parse_compile_plan(
    raw: str,
    parse: Callable,
    *,
    validate_updates: Callable[[CompilePlan], str | None] | None = None,
    repaired: bool = False,
    **options,
) -> CompilePlan:
    """Receive one delivered plan, classifying invalid syntax, shape or reading scope."""
    if not isinstance(raw, str):
        raise CompilePlanError("Plan response must be text", "concept_plan_unparseable")
    try:
        parsed = parse(raw)
    except ValueError as error:
        raise CompilePlanError(str(error), "concept_plan_unparseable") from error
    try:
        plan = normalize_plan(parsed, **options)
    except ValueError as error:
        raise CompilePlanError(str(error), "malformed_plan_items") from error
    diagnostic = validate_updates(plan) if validate_updates else None
    if diagnostic:
        raise CompilePlanError(diagnostic, "page_update_originals_unread")
    if repaired:
        report_compile_issue("compile_plan_repaired")
    elif plan.normalized:
        report_compile_issue("compile_plan_normalized")
    return plan


def plan_repair_message(error: CompilePlanError, raw: str) -> str:
    """The same pure correction is sent after one invalid plan, at most once."""
    if error.code == "page_update_originals_unread":
        return error.detail + "\nPrevious plan:\n" + raw
    return (
        "The previous JSON plan failed validation: "
        + error.detail
        + "\nReturn a corrected JSON plan with concepts and entities objects, each with "
        "create/update/related arrays. Use supported types and existing targets. "
        "Preserve valid proposals. Previous output:\n" + raw
    )


def request_compile_plan(
    request: Callable[[str | None], str],
    parse: Callable,
    *,
    validate_updates: Callable[[CompilePlan], str | None] | None = None,
    checkpoints=None,
    request_digest: str = "",
    **options,
) -> CompilePlan:
    correction = None
    if checkpoints:
        cached = checkpoints.load("plan", request_digest)
        if cached is not None:
            try:
                return parse_compile_plan(
                    cached, parse, validate_updates=validate_updates, **options
                )
            except CompilePlanError as error:
                checkpoints.record_failure("plan", request_digest, error.code)
                report_compile_issue(error.code, "concepts", "entities")
                raise
        reader = getattr(checkpoints, "retained_response", None)
        retained = (
            reader(
                "plan",
                request_digest,
                protocol="compile-plan-json",
                sections=(),
                stage="compile.concepts-plan",
            )
            if reader
            else None
        )
        if retained is not None:
            raw, origin = retained
            try:
                plan = parse_compile_plan(raw, parse, validate_updates=validate_updates, **options)
            except CompilePlanError as error:
                correction = plan_repair_message(error, raw)
            else:
                checkpoints.save("plan", request_digest, raw, reused_response=origin)
                return plan
        if correction is None:
            checkpoints.require_generation("plan", request_digest)
    for attempt in range(1 if correction else 0, 2):
        if attempt and checkpoints:
            checkpoints.claim_repair("plan", request_digest)
        elif checkpoints:
            checkpoints.claim_generation("plan", request_digest, stage="compile.concepts-plan")
        raw = request(correction)
        if checkpoints:
            checkpoints.record_received_response(
                "plan",
                request_digest,
                raw,
                stage="compile.concepts-plan",
                response_protocol="compile-plan-json",
                expected_sections=(),
                code="response_received",
                attempt=attempt,
            )
        try:
            plan = parse_compile_plan(
                raw, parse, validate_updates=validate_updates, repaired=bool(attempt), **options
            )
        except CompilePlanError as error:
            if checkpoints:
                checkpoints.record_invalid_response(
                    "plan",
                    request_digest,
                    raw,
                    stage="compile.concepts-plan",
                    response_protocol="compile-plan-json",
                    expected_sections=(),
                    code=error.code,
                    detail=error.detail,
                    attempt=attempt,
                )
            if attempt:
                if checkpoints:
                    checkpoints.record_failure("plan", request_digest, error.code)
                report_compile_issue(error.code, "concepts", "entities")
                raise
            correction = plan_repair_message(error, raw)
        else:
            if checkpoints:
                checkpoints.save("plan", request_digest, raw)
            return plan
    raise AssertionError("Unreachable plan retry state")
