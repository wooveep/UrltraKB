"""Sequential navigation requests share execution accounting and durable recovery."""

import json
from contextlib import nullcontext

from openkb.agent.evidence_wire import WireMessages
from openkb.agent.model_json import json_text
from openkb.agent.request_analysis import RequestAnalysis
from openkb.agent.source_protocol import SYSTEM, source_messages
from openkb.config import compilation_model_options
from openkb.navigation_enhancement import IndexAllowanceExceeded
from openkb.processing import InputTooLarge, OutputTruncated
from openkb.processing_reservation import reserve_later_work, retry_attempt_available


class _SummaryInvalid(ValueError):
    def __init__(self, path, reason):
        super().__init__(f"{path}: {reason}")


class _SummaryMessages(WireMessages):
    def decode_response(self, raw):
        # Retain object pairs until duplicate fields have been located, before
        # the shared wire decoder can collapse the diagnostic to a reason code.
        class ObjectPairs(list):
            pass

        def check(value, path):
            if isinstance(value, ObjectPairs):
                seen = set()
                for name, item in value:
                    field = (
                        f"{path}.{name}" if name.isidentifier() else f"{path}[{json.dumps(name)}]"
                    )
                    if name in seen:
                        raise _SummaryInvalid(field, "duplicate field")
                    seen.add(name)
                    check(item, field)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    check(item, f"{path}[{i}]")

        check(json.loads(json_text(raw), object_pairs_hook=ObjectPairs), "$")
        return super().decode_response(raw)


def _summary_fields(value, path, fields):
    if not isinstance(value, dict):
        raise _SummaryInvalid(path, "expected an object")
    for name in fields:
        if name not in value:
            raise _SummaryInvalid(f"{path}.{name}", "missing required field")
    for name in value:
        if name not in fields:
            raise _SummaryInvalid(f"{path}[{json.dumps(name)}]", "unexpected field")


def expand_capacity(allowance, reason):
    old = allowance.limits
    expanded = old.expanded(reason=reason)
    budget = allowance.budget
    if old == expanded:
        return False
    if not budget.expand(budget.limits, {}, reason):
        return False
    allowance.limits = expanded
    return True


def request_value(
    evidence,
    task,
    rules,
    settings,
    bundle,
    allowance,
    checkpoints,
    profile,
    validate,
    *,
    attempts=None,
):
    from openkb.agent.compiler import _llm_call

    request = source_messages(evidence, task, rules)
    if task["stage"] == "index_summary":
        request = _SummaryMessages(request, request.identities)
    options = {
        "max_tokens": min(allowance.limits.output_tokens, allowance.budget.limits.output_tokens),
        "response_format": {"type": "json_object"},
        **compilation_model_options(settings),
    }
    payload = {"request": list(request), "stage": task["stage"]}
    limit = allowance.limits.max_attempts if attempts is None else attempts
    family = reserve_later_work(attempts=limit) if attempts is None else nullcontext()
    with (
        family,
        checkpoints.request(
            SYSTEM, payload, dependencies={"profile": profile, "options": options}
        ) as key,
    ):
        saved = checkpoints.load(key)
        if saved is not None:
            if "invalid" in saved:
                raise IndexAllowanceExceeded(saved["invalid"])
            validate(saved)
            return saved
        for attempt in range(limit):
            options["max_tokens"] = min(
                allowance.limits.output_tokens, allowance.budget.limits.output_tokens
            )
            analysis = RequestAnalysis(
                checkpoints,
                task["stage"],
                request,
                options,
                rules=(__name__, "openkb.navigation_structure", "openkb.agent.source_protocol"),
            )

            def produce():
                allowance.request(settings["model"], request)
                return _llm_call(
                    settings["model"], request, task["stage"], bundle=bundle, **options
                )

            try:
                with allowance.enforce():
                    value = analysis.run(produce, validate)
                checkpoints.save(key, value)
                return value
            except (OutputTruncated, InputTooLarge) as exc:
                if (
                    attempt + 1 == limit
                    or not retry_attempt_available()
                    or not expand_capacity(allowance, exc.reason)
                ):
                    raise
            except (ValueError, TypeError, KeyError) as exc:
                reason = task["stage"] + "_invalid"
                if isinstance(exc, _SummaryInvalid):
                    reason += f": {exc}"
                elif task["stage"] == "index_summary":
                    reason += f": $: {exc}"
                if attempt + 1 < limit and retry_attempt_available():
                    request = source_messages(evidence, {**task, "recovery": reason}, rules)
                    if task["stage"] == "index_summary":
                        request = _SummaryMessages(request, request.identities)
                    continue
                checkpoints.save(key, {"invalid": reason})
                raise IndexAllowanceExceeded(reason) from None
    raise AssertionError("Navigation request loop must settle or raise")


def summarize_group(evidence, nodes, settings, bundle, allowance, checkpoints, profile):
    # Short numbers belong only to this request; persisted identities stay in code.
    by_number = {str(i): node for i, node in enumerate(nodes, 1)}
    selected = [
        {
            "id": number,
            "title": n["title"],
            "title_origin": n["title_origin"],
            "start": n["start"],
            "end": n["end"],
        }
        for number, n in by_number.items()
    ]
    pending = {row["id"] for row in selected}
    first_problem = None

    def validate(value):
        _summary_fields(value, "$", ("summaries",))
        if not isinstance(value["summaries"], list):
            raise _SummaryInvalid("$.summaries", "expected an array")

    rules = (
        'Return {"summaries":[{"id":"1","summary":"brief hint"}]}.'
        " Copy each supplied request-local section number exactly as a string."
        " Summarize only the supplied nodes; accepted summaries need no repetition."
        " Use null when evidence is insufficient. At most 80 words per hint."
        " Do not infer hierarchy or treat inferred titles as original evidence."
    )
    with reserve_later_work(attempts=allowance.limits.max_attempts):
        for attempt in range(allowance.limits.max_attempts):
            task = {"stage": "index_summary", "nodes": [r for r in selected if r["id"] in pending]}
            if attempt:
                task["recovery"] = first_problem or "Supply only the remaining summaries."
            problems, seen = [], set()
            try:
                value = request_value(
                    evidence,
                    task,
                    rules,
                    settings,
                    bundle,
                    allowance,
                    checkpoints,
                    profile,
                    validate,
                    attempts=1,
                )
                for i, row in enumerate(value["summaries"]):
                    path = f"$.summaries[{i}]"
                    try:
                        _summary_fields(row, path, ("id", "summary"))
                        if not isinstance(row["id"], str):
                            raise _SummaryInvalid(f"{path}.id", "expected a string section number")
                        if row["id"] not in pending:
                            raise _SummaryInvalid(f"{path}.id", "unknown section number")
                        if row["id"] in seen:
                            raise _SummaryInvalid(
                                f"{path}.id", f"duplicate section number {row['id']}"
                            )
                        if row["summary"] is not None:
                            if not isinstance(row["summary"], str):
                                raise _SummaryInvalid(
                                    f"{path}.summary", "expected a string or null"
                                )
                            if not 0 < len(row["summary"]) <= 1600:
                                raise _SummaryInvalid(
                                    f"{path}.summary", "expected 1 to 1600 characters"
                                )
                            by_number[row["id"]].update(
                                summary=row["summary"], summary_origin="model"
                            )
                        seen.add(row["id"])
                    except _SummaryInvalid as exc:
                        problems.append(str(exc))
                pending.difference_update(seen)
                if not pending:
                    return
                problems.append(
                    "$.summaries: missing section numbers: "
                    + ", ".join(number for number in by_number if number in pending)
                )
            except IndexAllowanceExceeded as exc:
                problems.append(str(exc).removeprefix("index_summary_invalid: "))
            first_problem = first_problem or problems[0]
            if not retry_attempt_available():
                break
    raise IndexAllowanceExceeded("index_summary_invalid: " + str(first_problem))
