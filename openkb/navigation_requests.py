"""Sequential navigation requests share execution accounting and durable recovery."""

from openkb.agent.request_analysis import RequestAnalysis
from openkb.agent.source_protocol import SYSTEM, source_messages
from openkb.config import compilation_model_options
from openkb.navigation_enhancement import IndexAllowanceExceeded
from openkb.processing import InputTooLarge, OutputTruncated


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
    evidence, task, rules, settings, bundle, allowance, checkpoints, profile, validate
):
    from openkb.agent.compiler import _llm_call

    request = source_messages(evidence, task, rules)
    options = {
        "max_tokens": min(allowance.limits.output_tokens, allowance.budget.limits.output_tokens),
        "response_format": {"type": "json_object"},
        **compilation_model_options(settings),
    }
    payload = {"request": list(request), "stage": task["stage"]}
    with checkpoints.request(
        SYSTEM, payload, dependencies={"profile": profile, "options": options}
    ) as key:
        saved = checkpoints.load(key)
        if saved is not None:
            if "invalid" in saved:
                raise IndexAllowanceExceeded(saved["invalid"])
            validate(saved)
            return saved
        for attempt in range(allowance.limits.max_attempts):
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
                if attempt + 1 == allowance.limits.max_attempts or not expand_capacity(
                    allowance, exc.reason
                ):
                    raise
            except (ValueError, TypeError, KeyError):
                reason = task["stage"] + "_invalid"
                checkpoints.save(key, {"invalid": reason})
                raise IndexAllowanceExceeded(reason) from None
    raise AssertionError("Navigation request loop must settle or raise")


def summarize_group(evidence, nodes, settings, bundle, allowance, checkpoints, profile):
    selected = [
        {
            "id": n["id"],
            "title": n["title"],
            "title_origin": n["title_origin"],
            "start": n["start"],
            "end": n["end"],
        }
        for n in nodes
    ]
    wanted = {row["id"] for row in selected}

    def validate(value):
        if (
            not isinstance(value, dict)
            or set(value) != {"summaries"}
            or not isinstance(value["summaries"], list)
        ):
            raise ValueError("Invalid summaries")
        seen = set()
        for row in value["summaries"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "summary"}
                or not isinstance(row["id"], str)
                or row["id"] not in wanted
                or row["id"] in seen
                or (
                    row["summary"] is not None
                    and (not isinstance(row["summary"], str) or not 0 < len(row["summary"]) <= 1600)
                )
            ):
                raise ValueError("Invalid summary reference")
            seen.add(row["id"])
        if seen != wanted:
            raise ValueError("Incomplete summaries")

    value = request_value(
        evidence,
        {"stage": "index_summary", "nodes": selected},
        'Return {"summaries":[{"id":"supplied node id","summary":"brief hint"}]}.'
        " Include every node once; use null if evidence is insufficient. At most "
        "80 words per hint. Do not infer new hierarchy or treat inferred titles "
        "as evidence. Summaries only help readers select original text.",
        settings,
        bundle,
        allowance,
        checkpoints,
        profile,
        validate,
    )
    summaries = {row["id"]: row["summary"] for row in value["summaries"]}
    for node in nodes:
        if summaries[node["id"]] is not None:
            node.update(summary=summaries[node["id"]], summary_origin="model")
