"""Schedule closed, unfinished ranges independently of their discovery window."""

from openkb.agent.document_range_validation import merged_intervals
from openkb.agent.evidence_retry import ResponseIncomplete
from openkb.navigation_enhancement import IndexAllowanceExceeded, record_optional_failure
from openkb.navigation_evidence import evidence_descriptor, read_evidence_group
from openkb.navigation_metadata import structure_diagnostics
from openkb.navigation_requests import summarize_group, summary_fits
from openkb.navigation_summary_inputs import gaps, parent_input, usable
from openkb.processing import (
    InputTooLarge,
    OutputTruncated,
    ProcessingIncomplete,
    processing_checkpoint,
)
from openkb.sources import content_id


def _outcome(node, status, reason=None, covered=(), *, basis=None):
    node["summary_details"] = {
        "status": status,
        "reason": reason,
        "covered_ranges": list(covered),
        **({"basis": basis} if basis is not None else {}),
    }


def summarize_pending(
    kb,
    source,
    parsed,
    record,
    evidence,
    end,
    settings,
    bundle,
    allowance,
    checkpoints,
    manifest,
    *,
    reader,
):
    nodes = record["nodes"]

    def diagnostics(targets):
        return structure_diagnostics(
            record, [manifest], ranges=[(n["start"], n["end"]) for n in targets]
        )

    selected = [
        n
        for n in nodes
        if (n["parent"] is not None or len(nodes) == 1)
        and n["end"] <= end
        and "summary_details" not in n
        and n["summary_origin"] != "model"
    ]

    def fail(error, pending):
        reason = getattr(error, "reason", str(error))
        status = "invalid"
        if (
            isinstance(error, (InputTooLarge, OutputTruncated))
            or reason == "index_allowance_exhausted"
        ):
            status = "budget_exceeded"
        elif isinstance(error, ProcessingIncomplete) and not isinstance(error, ResponseIncomplete):
            status = "interrupted"
        for node in pending:
            if "summary_details" not in node:
                _outcome(node, status, reason)
        record_optional_failure(record, error)
        manifest.update(status="basic", reason=reason)

    def read(start, stop):
        return read_evidence_group(
            kb, source, parsed, evidence_descriptor(source, parsed, start, stop), reader=reader
        )

    def request(group, targets, summary_input=None):
        try:
            summarize_group(
                group,
                targets,
                settings,
                bundle,
                allowance,
                checkpoints,
                record["profile"],
                summary_input=summary_input,
                structure_issues=diagnostics(targets),
            )
        except (IndexAllowanceExceeded, ProcessingIncomplete) as exc:
            fail(exc, targets)

    def individual(node):
        # Find a fitting original range with logarithmic probes; never label a
        # prefix summary as if it covered the rest of the section.
        start, stop = node["start"], node["end"]
        pieces = []
        while start < stop:
            processing_checkpoint("index_summary")

            def candidate(middle):
                part = {**node, "start": start, "end": middle}
                group = read(start, middle)
                if fits(group, [part]):
                    return group, part
                return None

            low, high = start + 1, stop
            chosen = candidate(low)
            step = 2
            while chosen is not None and low < high:
                middle = min(start + step, high)
                value = candidate(middle)
                if value is None:
                    high = middle - 1
                    break
                low, chosen = middle, value
                step *= 2
            while chosen is not None and low < high:
                middle = (low + high + 1) // 2
                value = candidate(middle)
                if value is not None:
                    low, chosen = middle, value
                else:
                    high = middle - 1
            if chosen is None:
                part = {**node, "start": start, "end": start + 1}
                fail(InputTooLarge(), [part])
                low = start + 1
            else:
                group, part = chosen
                request(group, [part])
            pieces.append(part)
            start = low
        if len(pieces) == 1:
            for key in ("summary", "summary_origin", "summary_details"):
                node[key] = pieces[0][key]
            return
        covered, hints = [], []
        for part in pieces:
            if usable(part):
                covered.extend(part["summary_details"]["covered_ranges"])
                hints.append(f"[{part['start']},{part['end']}): {part['summary']}")
        covered = [list(span) for span in merged_intervals(covered)]
        complete = covered == [[node["start"], node["end"]]]
        reason = next(
            (p["summary_details"]["reason"] for p in pieces if p["summary_details"]["reason"]), None
        )
        status = (
            "complete"
            if complete
            else "partial"
            if hints
            else pieces[0]["summary_details"]["status"]
        )
        node.update(summary="\n".join(hints), summary_origin="model" if hints else "unavailable")
        _outcome(node, status, reason, covered, basis="original")

    def parent(node, children):
        # Immediate child ranges are disjoint. Never count a child and all of
        # its descendants twice, or reread a null child's body as a hidden retry.
        direct = gaps(node["start"], node["end"], [[c["start"], c["end"]] for c in children])
        group = {
            "group_id": content_id({"parent": [node["start"], node["end"]], "direct": direct}),
            "source_id": source.source_id,
            "version_id": source.id,
            "parse_id": parsed.id,
            "document": source.name,
            "blocks": [b for left, right in direct for b in read(left, right)["blocks"]],
        }
        supplied = [
            list(span)
            for span in merged_intervals([(b["order"], b["order"] + 1) for b in group["blocks"]])
        ]
        inputs = parent_input(node, children, [], supplied)
        if not fits(group, [node], inputs):
            # Large direct introductions still use the same bounded original
            # fragment path. Accepted fragments survive a failed parent merge.
            pieces = []
            for left, right in direct:
                part = {**node, "start": left, "end": right}
                individual(part)
                pieces.append(part)
            group = {**group, "blocks": []}
            inputs = parent_input(node, children, pieces, [])
        if fits(group, [node], inputs):
            request(group, [node], inputs)
        else:
            fail(InputTooLarge(), [node])
        node["summary_details"].update(
            basis=inputs["basis"], input_signature=inputs["input_signature"]
        )

    def fits(group, targets, inputs=None):
        return summary_fits(
            group,
            targets,
            settings,
            allowance,
            summary_input=inputs,
            structure_issues=diagnostics(targets),
        )

    def schedule(group, targets):
        if fits(group, targets):
            request(group, targets)
        elif len(targets) > 1:
            middle = len(targets) // 2
            schedule(group, targets[:middle])
            schedule(group, targets[middle:])
        else:
            individual(targets[0])

    bounds = [b["order"] for b in evidence["blocks"]]
    local = [n for n in selected if bounds and min(bounds) <= n["start"] and n["end"] <= end]
    if local:
        schedule(evidence, local)
    for node in sorted(selected, key=lambda n: (n["end"] - n["start"], -n["start"])):
        if node in local:
            continue
        children = [n for n in nodes if n["parent"] == node["id"]]
        if (
            children
            and all(n.get("structure") and n["title_origin"] == "source" for n in children)
            and any(usable(n) for n in children)
        ):
            parent(node, children)
        else:
            # Wide leaves have no trustworthy substructure to synthesize.
            individual(node)


def finish_summary_states(nodes, enabled):
    for node in nodes:
        if "summary_details" not in node:
            required = enabled and (node["parent"] is not None or len(nodes) == 1)
            _outcome(
                node,
                "not_scheduled" if required else "not_requested",
                "index_summary_not_scheduled" if required else None,
            )
