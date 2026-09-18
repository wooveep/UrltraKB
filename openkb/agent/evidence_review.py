"""Nonblocking review notices, retained across generation and topic reuse."""

from openkb.compilation_report import collect_compile_report, report_auxiliary_warning
from openkb.processing import ProcessingIncomplete

ACCEPTED = {"supported", "advisory"}
KINDS = {"presentation", "coverage", "uncertainty"}


def validate_advisories(review, payload):
    rows = review.get("advisories", [])
    invalid = ProcessingIncomplete("evidence_verification_invalid", "generation")
    if not isinstance(rows, list):
        raise invalid
    ids = {item["id"] for item in payload["occurrences"]}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"kind", "candidate", "occurrences", "reason"}:
            raise invalid
        refs, candidate = row["occurrences"], row["candidate"]
        if (
            not isinstance(row["kind"], str)
            or row["kind"] not in KINDS
            or not isinstance(candidate, str)
            or (candidate and candidate not in payload["content"])
            or not isinstance(refs, list)
            or any(not isinstance(ref, str) or ref not in ids for ref in refs)
            or (row["kind"] != "presentation" and not refs)
            or not isinstance(row["reason"], str)
            or not row["reason"].strip()
        ):
            raise invalid
    uncertain = any(row["kind"] == "uncertainty" for row in rows)
    if (review["verdict"] == "advisory" and not uncertain) or (
        review["verdict"] == "supported" and uncertain
    ):
        raise invalid
    return rows


def record_review(path, review, evidence):
    from openkb.agent.evidence_generation_protocol import source_mapping

    identities = {row["id"]: row["fact_id"] for row in source_mapping(evidence)["occurrences"]}
    notes = [
        {"kind": row["kind"], "facts": sorted({identities[ref] for ref in row["occurrences"]})}
        for row in review.get("advisories", [])
    ]
    restore_notes(path, notes, set(identities.values()))


def restore_notes(path, notes, fact_ids):
    """Replay notices on cache hits; unreviewed/omitted facts never become verified."""
    if not isinstance(notes, list):
        raise ValueError("Invalid review notices")
    for note in notes:
        if (
            not isinstance(note, dict)
            or set(note) != {"kind", "facts"}
            or not isinstance(note["kind"], str)
            or note["kind"] not in KINDS
            or not isinstance(note["facts"], list)
            or any(not isinstance(fid, str) or fid not in fact_ids for fid in note["facts"])
            or (note["kind"] != "presentation" and not note["facts"])
        ):
            raise ValueError("Invalid review notice binding")
    with collect_compile_report() as report:
        for note in notes:
            recorded = report.review_notes.setdefault(path, [])
            if note not in recorded:
                recorded.append(note)
            report_auxiliary_warning("knowledge_review_" + note["kind"])


def unverified_facts(report):
    return {
        fid
        for notes in report.review_notes.values()
        for note in notes
        if note["kind"] in {"coverage", "uncertainty"}
        for fid in note["facts"]
    }


def retain_review_notes(report, paths):
    report.review_notes = {
        path: notes for path, notes in report.review_notes.items() if path in paths
    }
    codes = {"knowledge_review_" + kind for kind in KINDS}
    report.warnings[:] = [code for code in report.warnings if code not in codes]
    report.warnings.extend(
        sorted(
            {
                "knowledge_review_" + note["kind"]
                for notes in report.review_notes.values()
                for note in notes
            }
        )
    )


def review_notice(report):
    if not report.review_notes:
        return ""
    labels = {
        "presentation": "表达可整理",
        "coverage": "有次要遗漏",
        "uncertainty": "存在待复核内容",
    }
    return (
        "\n\n## 内容复核提示\n\n"
        "> 以下知识已保留，可结合原文检查并编辑；相关提示会在继续处理时保留。\n\n"
        + "\n".join(
            f"- [[{path}]]：" + "、".join(sorted({labels[n["kind"]] for n in notes}))
            for path, notes in sorted(report.review_notes.items())
        )
    )


def stored_review_warnings(document):
    import json

    warnings = json.loads(document.get("compilation_review_warnings", "[]"))
    if not isinstance(warnings, list) or any(
        not isinstance(code, str) or code not in {"knowledge_review_" + kind for kind in KINDS}
        for code in warnings
    ):
        raise ValueError("Invalid stored review warnings")
    return tuple(warnings)
