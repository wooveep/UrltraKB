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
from openkb.sources import content_id

SUMMARY_RULES = (
    'Return {"summaries":[{"id":"1","summary":"brief hint"}]}.'
    " Copy each supplied request-local section number exactly as a string."
    " Summarize only the supplied nodes; accepted summaries need no repetition."
    " Use null when evidence is insufficient. At most 80 words per hint."
    " Do not infer hierarchy or treat inferred titles as original evidence."
    " Ranges describe navigation subtrees, not exclusive ownership;"
    " preserve supplied structure gaps."
)
MERGE_RULES = SUMMARY_RULES + (
    " This is a parent navigation summary. summary_input contains explicitly derived hints,"
    " not original quotations; evidence contains only the parent's direct original material."
    " Synthesize only these supplied inputs, preserving main themes and conditions."
    " Missing or null child hints are gaps; do not infer their contents or semantic completeness."
    " Return a short navigation hint, not an overview, plan, concatenated hints or page body."
)


def summary_task(nodes, summary_input=None, structure_issues=()):
    return {
        "stage": "index_summary",
        "nodes": [
            {"id": str(i), **{key: n[key] for key in ("title", "title_origin", "start", "end")}}
            for i, n in enumerate(nodes, 1)
        ],
        **({"summary_input": summary_input} if summary_input is not None else {}),
        **({"structure_issues": structure_issues} if structure_issues else {}),
    }


def summary_fits(evidence, nodes, settings, allowance, *, summary_input=None, structure_issues=()):
    return allowance.fits(
        settings["model"],
        source_messages(
            evidence,
            summary_task(nodes, summary_input, structure_issues),
            MERGE_RULES if summary_input is not None else SUMMARY_RULES,
        ),
    )


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
    reconsider=None,
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
                followup = reconsider(value) if reconsider is not None else None
                if followup and attempt + 1 < limit and retry_attempt_available():
                    request = source_messages(evidence, {**task, **followup}, rules)
                    continue
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


def summarize_group(
    evidence,
    nodes,
    settings,
    bundle,
    allowance,
    checkpoints,
    profile,
    *,
    summary_input=None,
    structure_issues=(),
):
    # Short numbers belong only to this request; persisted identities stay in code.
    by_number = {str(i): node for i, node in enumerate(nodes, 1)}
    selected = summary_task(nodes)["nodes"]
    pending = {row["id"] for row in selected}
    first_problem = None
    rules = MERGE_RULES if summary_input is not None else SUMMARY_RULES
    dependencies = {
        "profile": profile,
        "options": compilation_model_options(settings),
        "max_tokens": min(allowance.limits.output_tokens, allowance.budget.limits.output_tokens),
    }
    evidence_id = content_id(evidence)

    def work_payload(node):
        return {
            "stage": "index_summary_item",
            "evidence": evidence_id,
            "node": {key: node[key] for key in ("title", "title_origin", "start", "end")},
            "summary_input": summary_input,
            "structure_issues": structure_issues,
        }

    for number, node in by_number.items():
        with checkpoints.request(rules, work_payload(node), dependencies=dependencies) as key:
            saved = checkpoints.load(key)
        if saved is not None:
            from openkb.navigation_metadata import validate_metadata

            validate_metadata({**node, **saved})
            node.update(saved)
            pending.remove(number)
    if not pending:
        return

    def validate(value):
        _summary_fields(value, "$", ("summaries",))
        if not isinstance(value["summaries"], list):
            raise _SummaryInvalid("$.summaries", "expected an array")

    with reserve_later_work(attempts=allowance.limits.max_attempts):
        for attempt in range(allowance.limits.max_attempts):
            task = {"stage": "index_summary", "nodes": [r for r in selected if r["id"] in pending]}
            if summary_input is not None:
                task["summary_input"] = summary_input
            if structure_issues:
                task["structure_issues"] = structure_issues
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
                            if not row["summary"].strip() or len(row["summary"]) > 1600:
                                raise _SummaryInvalid(
                                    f"{path}.summary", "expected 1 to 1600 characters"
                                )
                            by_number[row["id"]].update(
                                summary=row["summary"], summary_origin="model"
                            )
                        node = by_number[row["id"]]
                        from openkb.agent.document_range_validation import merged_intervals

                        original_ranges = [
                            list(span)
                            for span in merged_intervals(
                                [
                                    (b["order"], b["order"] + 1)
                                    for b in evidence.get("blocks", [])
                                    if node["start"] <= b.get("order", -1) < node["end"]
                                ]
                            )
                        ]
                        covered = (
                            (
                                summary_input["covered_ranges"]
                                if summary_input is not None
                                else original_ranges
                            )
                            if row["summary"] is not None
                            else []
                        )
                        if row["summary"] is None:
                            node.update(summary="", summary_origin="unavailable")
                        complete = merged_intervals(covered) == [(node["start"], node["end"])]
                        node["summary_details"] = {
                            "status": "complete"
                            if complete
                            else "partial"
                            if covered
                            else "insufficient_evidence",
                            "reason": None
                            if complete
                            else "index_summary_partial"
                            if covered
                            else "index_summary_empty",
                            "covered_ranges": covered,
                            "basis": summary_input["basis"]
                            if summary_input is not None
                            else "original",
                            **(
                                {"input_signature": summary_input["input_signature"]}
                                if summary_input is not None
                                else {}
                            ),
                        }
                        with checkpoints.request(
                            rules, work_payload(node), dependencies=dependencies
                        ) as key:
                            checkpoints.save(
                                key,
                                {
                                    field: node[field]
                                    for field in ("summary", "summary_origin", "summary_details")
                                },
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
