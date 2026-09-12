"""Lossless source grouping and a checked wire format for mixed-source topics.

Fact IDs identify evidence; occurrence IDs distinguish repeated windows. The
wire format is generation-specific so unchanged facts and plans remain reusable.
"""

import json
import re
from collections import Counter
from copy import deepcopy

from openkb.agent.evidence_retry import ResponseIncomplete
from openkb.agent.evidence_units import JSON_FORMAT
from openkb.agent.evidence_units import messages as base_messages
from openkb.config import compilation_model_options
from openkb.processing import ProcessingIncomplete

SCOPED_CONTRACT = (
    'Return JSON {"title":"neutral topic covering all tasks","covered":[every fact id],'
    '"fragments":[{"scope":"s1","occurrences":["e1"],"heading":"task label",'
    '"content":"Markdown for these occurrences"}]}. '
    "Use each occurrence exactly once, only inside its assigned source scope. "
    "Keep every fact id in covered, including repeated ids. Do not return a separate content. "
    "Keep short labels verbatim, even when other facts contain commands. "
    "Reproduce commands exactly; avoid extra explanations of their purpose or effect. "
    "Do not introduce before/after relationships absent from the original wording. "
    "Do not turn different tasks into prerequisites for one another. "
    "Use concise natural task headings; do not print provenance IDs or full source paths. "
    "The application joins fragments as sibling task sections in this single knowledge page. "
    "Keep title exactly unchanged when title_fixed is true."
)


def source_mapping(evidence):
    groups, occurrences = {}, []
    for number, item in enumerate(evidence, 1):
        location = item.get("location", {})
        headings = list(location.get("headings", []))
        if not headings:
            headings = [
                p["text"] for p in item.get("neighbors", []) if p.get("relation") == "heading"
            ]
        origin, nested = [], location
        while isinstance(nested.get("attachment"), dict):
            attachment = nested["attachment"]
            origin.append({k: attachment.get(k) for k in ("part", "name")})
            nested = attachment.get("position", {})
            headings.extend(nested.get("headings", []))
        scope = {"origin": origin or "enclosing_document", "headings": headings}
        ref = item.get("reference", {})
        identity = {k: ref[k] for k in ("source_id", "version_id", "parse_id") if k in ref}
        key = json.dumps([identity, scope], ensure_ascii=False, sort_keys=True)
        group = groups.setdefault(key, {"id": f"s{len(groups) + 1}", **scope, "occurrences": []})
        occurrence = {"id": f"e{number}", "fact_id": item["id"], "scope": group["id"]}
        group["occurrences"].append(occurrence["id"])
        occurrences.append(occurrence)
    return {"source_scopes": list(groups.values()), "occurrences": occurrences}


def generation_payload(base, facts, evidence):
    result = {
        **{
            key: value
            for key, value in base.items()
            if key not in {"_topic_fact_ids", "_title_context"}
        },
        "facts": facts,
        "evidence": evidence,
    }
    if base.get("_title_context") and {fact["id"] for fact in facts} != base["_topic_fact_ids"]:
        result["title_context"] = base["_title_context"]
    mapping = source_mapping(evidence)
    if len(mapping["source_scopes"]) > 1:
        result.update(mapping)
    return result


def messages(system, payload):
    result = base_messages(system, payload)
    wire = json.loads(result[-1]["content"])
    if payload.get("stage") == "generation" and payload.get("source_scopes"):
        wire["output_contract"] = SCOPED_CONTRACT
    if payload.get("stage") == "generation" and payload.get("revision"):
        if not payload.get("title_fixed"):
            # A rejected title belongs to the rejected candidate. Repeating it
            # as the requested title competes with the review's correction.
            wire.pop("title", None)
            wire["title_instruction"] = (
                "Return a corrected, source-faithful neutral title in the title field. "
                "revision.title is rejected candidate data, not a required title. "
                "Resolve the review's specific discrepancy. Preserve every required "
                "quote and keep each operation bound to its original target. "
                "Preserve short source labels as literal quotations in the body; "
                "do not promote a quoted label into a heading governing a different "
                "operation. Use neutral section headings when the original label "
                "and the adjacent operation conflict."
            )
    if payload.get("stage") == "generation" and payload.get("title_context"):
        # Other parts' originals are review-only title evidence. Exposing them
        # to a body writer invites valid quotations to leak across part scopes.
        # A title-only repair is safe: the application retains the exact body.
        if not title_only_correction(payload):
            wire.pop("title_context", None)
        wire["output_contract"] += (
            " The supplied public title is shared with other topic parts. "
            "Write the current body only from its own facts and evidence; do not "
            "import another part's facts or transfer its operation or conditions. "
            "Do not expand this body to cover every task named in the shared title. "
            "Keep the public title unchanged when title_fixed is true."
        )
    if isinstance(wire.get("revision"), dict):
        wire["revision"] = {k: v for k, v in wire["revision"].items() if k != "candidate"}
    if title_only_correction(payload):
        wire["output_contract"] = (
            'Return only {"title":"corrected neutral title"}. '
            "All located issues concern the public title. The application retains "
            "the previously reviewed body exactly; do not rewrite or expand it."
        )
    result[-1]["content"] = json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
    return result


def title_only_correction(payload):
    revision = payload.get("revision")
    if not isinstance(revision, dict) or payload.get("title_fixed"):
        return False
    issues = revision.get("issues")
    title = revision.get("title")
    return (
        isinstance(title, str)
        and isinstance(issues, list)
        and bool(issues)
        and all(
            isinstance(issue, dict)
            and issue.get("kind") == "title"
            and isinstance(issue.get("candidate"), str)
            and bool(issue["candidate"])
            and issue["candidate"] in title
            for issue in issues
        )
        and (isinstance(revision.get("candidate"), dict) or not payload.get("source_scopes"))
    )


def apply_title_correction(output, payload):
    """An explicitly located title repair cannot mutate the reviewed body."""
    if not title_only_correction(payload):
        return output
    if (
        not isinstance(output, dict)
        or not isinstance(output.get("title"), str)
        or not output["title"].strip()
    ):
        raise ResponseIncomplete("topic_generation_incomplete", "generation")
    revision = payload["revision"]
    candidate = deepcopy(revision.get("candidate"))
    if candidate is None:
        # Older single-scope drafts already retained the exact reviewed body.
        candidate = {"content": revision["content"], "covered": [f["id"] for f in payload["facts"]]}
    old, title = revision["title"], output["title"]
    if candidate.get("fragments"):
        if candidate["fragments"][0]["heading"] == old:
            candidate["fragments"][0]["heading"] = title
            candidate.pop("content", None)
    else:
        candidate["content"] = re.sub(
            r"\A(#{1,6})[ \t]+" + re.escape(old) + r"[ \t]*(?=\n|$)",
            lambda match: f"{match[1]} {title}",
            candidate["content"],
            count=1,
        )
    return {**candidate, "title": title}


def generation_options(settings, *, correction=False):
    """An explicit correction mode leaves the normal generation mode unchanged."""
    mode = settings.get("correction_thinking") if correction else None
    adjusted = {**settings, "compilation_thinking": mode} if mode is not None else settings
    return compilation_model_options(adjusted)


def fits(limits, model, system, payload):
    try:
        limits.request(model, messages(system, payload), {"response_format": JSON_FORMAT})
        return True
    except ProcessingIncomplete as exc:
        if exc.reason != "input_budget_exceeded":
            raise
        return False


def _body_headings(content):
    """Keep fragment headings local without changing fenced command examples."""
    lines, fence = [], None
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\n"))
        if marker:
            run, suffix = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not suffix.strip():
                fence = None
        elif fence is None:
            line = re.sub(r"^ {0,3}#{1,2}(?=\s)", "###", line)
            # Setext headings also must not introduce a new enclosing task.
            if re.fullmatch(r" {0,3}(?:=+|-+)\s*", line) and lines and lines[-1].strip():
                lines[-1] = "### " + lines[-1].lstrip()
                continue
        lines.append(line)
    if fence is not None:
        raise ResponseIncomplete("topic_generation_incomplete", "generation")
    return "".join(lines).strip()


def normalize_output(output, payload):
    """Validate before deriving the internal Markdown representation or receipts."""
    if not payload.get("source_scopes"):
        return output
    invalid = ResponseIncomplete("topic_generation_incomplete", "generation")
    if not isinstance(output, dict) or not isinstance(output.get("fragments"), list):
        raise invalid
    covered = output.get("covered")
    if (
        not isinstance(covered, list)
        or any(not isinstance(v, str) for v in covered)
        or Counter(covered) != Counter(f["id"] for f in payload["facts"])
    ):
        raise invalid
    expected = {o["id"]: o["scope"] for o in payload["occurrences"]}
    assigned, sections = [], []
    for fragment in output["fragments"]:
        if not isinstance(fragment, dict):
            raise invalid
        ids, scope = fragment.get("occurrences"), fragment.get("scope")
        heading, content = fragment.get("heading"), fragment.get("content")
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(v, str) or v not in expected or expected[v] != scope for v in ids)
            or not isinstance(heading, str)
            or not heading.strip()
            or "\n" in heading
            or "\r" in heading
            or not isinstance(content, str)
            or not content.strip()
        ):
            raise invalid
        assigned.extend(ids)
        sections.append("## " + heading.strip().lstrip("# ") + "\n\n" + _body_headings(content))
    if Counter(assigned) != Counter(expected.keys()):
        raise invalid
    content = "\n\n".join(sections)
    if "content" in output and output["content"] != content:
        raise invalid
    return {**output, "content": content}


def fragment_bindings(output):
    """Point the reviewer to the assembled section for each evidence occurrence."""
    return [
        {
            "section": index,
            "heading": f["heading"],
            "scope": f["scope"],
            "occurrences": f["occurrences"],
        }
        for index, f in enumerate(output.get("fragments", []), 1)
    ]


def representative_output(payload):
    """Budget a complete wire response, including the new protocol's fields."""
    result = {"title": payload["title"], "covered": [f["id"] for f in payload["facts"]]}
    if not payload.get("source_scopes"):
        return {**result, "content": "\n\n".join(e["text"] for e in payload["evidence"])}
    texts = {o["id"]: e["text"] for o, e in zip(payload["occurrences"], payload["evidence"])}
    result["fragments"] = [
        {
            "scope": scope["id"],
            "occurrences": scope["occurrences"],
            "heading": "Source task",
            "content": "\n\n".join(texts[oid] for oid in scope["occurrences"]),
        }
        for scope in payload["source_scopes"]
    ]
    return result
