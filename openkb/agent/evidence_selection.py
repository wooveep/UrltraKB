"""Account for secondary detail kept in the source instead of repeating it in prose."""

from openkb.compilation_report import collect_compile_report

SELECTION_POLICY = """Write a concise knowledge page centered on the core concept, useful
conclusions, key steps and necessary restrictions. Combine repetition and prefer a coherent
explanation over a transcript. Do not invent a length or page-count target.
Secondary background, repeated examples and incidental labels may stay in the original
source. Declare their occurrence IDs in source_details; the application supplies a source
link. covered accounts for all input facts, either in the prose or through source_details;
it does not require every quotation to be reproduced. A detail-only batch may have no prose.
Never defer a core fact or a prerequisite, exception, version, negation, parameter or command
needed to understand or perform the retained task correctly. Keep required steps in order.
For a fact mixing essential and secondary information, retain its essential meaning in
concise prose; do not mark the whole occurrence as source_details. If unsure whether it is needed,
retain it concisely. Native table rows and relationships must remain intact when reproduced;
do not defer individual table cells or row batches. The originals remain authoritative.
source_details is a selection decision for review, never proof that an omission is safe."""

DETAIL_CONTRACT = (
    ' Optional "source_details":["e2", ...] lists supplied occurrence IDs whose secondary '
    "detail is intentionally left in the original, linked by the application. Keep all fact "
    "IDs in covered. Do not print these IDs or a coverage checklist in the prose."
)


def detail_occurrences(output, payload):
    """Validate explicit selections against actual evidence, including repeated windows."""
    from openkb.agent.evidence_generation_protocol import source_mapping
    from openkb.agent.evidence_retry import ResponseIncomplete

    invalid = ResponseIncomplete("topic_generation_incomplete", "generation")
    if not isinstance(output, dict):
        raise invalid
    details = output.get("source_details", [])
    occurrences = source_mapping(payload["evidence"])["occurrences"]
    ids = {row["id"]: row["fact_id"] for row in occurrences}
    tables = {fact["id"] for fact in payload["facts"] if "table_object" in fact}
    for table in payload.get("table_objects", []):
        tables.update(cell["fact_id"] for cell in table["cells"])
    if (
        not isinstance(details, list)
        or any(not isinstance(ref, str) or ref not in ids for ref in details)
        or len(set(details)) != len(details)
        or any(ids[ref] in tables for ref in details)
    ):
        raise invalid
    return details


def record_details(path, details, evidence):
    from openkb.agent.evidence_generation_protocol import source_mapping

    identities = {row["id"]: row["fact_id"] for row in source_mapping(evidence)["occurrences"]}
    restore_details(path, sorted({identities[ref] for ref in details}), set(identities.values()))


def restore_details(path, facts, allowed):
    if (
        not isinstance(facts, list)
        or any(not isinstance(fid, str) or fid not in allowed for fid in facts)
        or len(set(facts)) != len(facts)
    ):
        raise ValueError("Invalid source detail receipt")
    if facts:
        with collect_compile_report() as report:
            report.referenced_facts.setdefault(path, set()).update(facts)


def referenced_facts(report):
    return {fid for facts in report.referenced_facts.values() for fid in facts}


def link_source_details(content, source_id, name, language):
    """One stable source link per contribution; exact evidence comments remain available."""
    from urllib.parse import quote

    label = (
        "补充细节见原文"
        if language.lower().startswith(("zh", "chinese", "中文"))
        else "Details in source"
    )
    link = f"[{label}](../sources/{quote(name, safe='')}.md)"
    closing = f"<!-- /openkb-source:{source_id} -->"
    return content.replace(closing, "\n" + link + "\n" + closing, 1)
