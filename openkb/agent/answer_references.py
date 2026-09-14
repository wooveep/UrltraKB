"""Render only short evidence references actually returned by source-reading tools."""

import hashlib
import json
import re

from markdown_it import MarkdownIt

from openkb.agent.answer_result import RenderedAnswer
from openkb.agent.evidence_markup import rewrite_inline

_MARKER = re.compile(r"\[evidence:([^\]\r\n]+)\]")
_CANONICAL = re.compile(
    r"\[原文\]\(sources/snapshots/[0-9a-f]{64}-[0-9a-f]{64}\.md#block-[0-9a-f]{64}\)"
)


def short_citation(citation):
    return "[evidence:" + hashlib.sha256(citation.encode()).hexdigest()[:16] + "]"


def _observed(result):
    calls, bindings = {}, {}
    for item in result.to_input_list():
        if item.get("type") == "function_call":
            calls[item.get("call_id", item.get("id"))] = item.get("name")
        if item.get("type") != "function_call_output":
            continue
        if calls.get(item.get("call_id", item.get("id"))) not in {
            "read_source_node",
            "search_source_text",
        }:
            continue
        try:
            value = json.loads(item.get("output", ""))
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or not isinstance(value.get("evidence"), list):
            continue
        for row in value["evidence"]:
            if not isinstance(row, dict):
                continue
            citation, marker = row.get("citation"), row.get("short_citation")
            if not isinstance(citation, str) or not _CANONICAL.fullmatch(citation):
                continue
            if marker != short_citation(citation):
                continue
            bindings.setdefault(marker, set()).add(citation)
    # A collision remains unresolved. Neither order nor similarity chooses a target.
    return {
        marker: next(iter(targets)) for marker, targets in bindings.items() if len(targets) == 1
    }


def render_references(answer, bindings):
    """Keep code examples, escaped syntax, table layout and unrelated bytes intact."""
    markdown = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    environment = {}
    tokens = markdown.parse(answer, environment)
    prose, replacements, unresolved = "", [], []

    def reference(state, silent):
        match = _MARKER.match(state.src, state.pos)
        if not match:
            return False
        if not silent:
            state.push("text", "", 0).content = match[0]
            if state.src is prose:
                if match[0] in bindings:
                    replacements.append((state.pos, match.end(), bindings[match[0]]))
                else:
                    unresolved.append(match[0])
        state.pos = match.end()
        return True

    markdown.inline.ruler.before("escape", "evidence_reference", reference)

    def parse_inline(text):
        nonlocal prose, replacements
        prose, replacements = text, []
        markdown.inline.parse(prose, markdown, environment, [])
        return replacements

    return rewrite_inline(answer, tokens, parse_inline), sorted(set(unresolved))


def resolve_references(result):
    answer = getattr(result, "final_output", None)
    if not isinstance(answer, str) or "[evidence:" not in answer:
        return result
    rendered, _ = render_references(answer, _observed(result))
    return RenderedAnswer(result, rendered) if rendered != answer else result
