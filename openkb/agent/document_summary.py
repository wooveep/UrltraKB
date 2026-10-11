"""Build ordered document knowledge from all original ranges, independently of page plans."""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openkb.agent.source_windows import CompileSourceWindows
from openkb.execution_budget import admit_document_summary
from openkb.source_context import COMPILATION_FACT_RULES

logger = logging.getLogger(__name__)
PROMPT_VERSION = "compiler-document-sections-v4"
SYSTEM_TEMPLATE = (
    "You are OpenKB's wiki compilation agent for document knowledge. "
    "Write independently readable knowledge in {language}.\n" + COMPILATION_FACT_RULES
)


def summary_capacity(
    config: dict,
    *,
    custom_endpoint: bool,
    tokenizer_snapshot=None,
    generation_options: dict | None = None,
) -> Callable[[list[dict]], bool]:
    from openkb.compilation_report import report_compile_issue
    from openkb.execution_capacity import capacity_policy
    from openkb.model_tokenizer import TokenMeasure

    policy = capacity_policy(
        config, custom_endpoint=custom_endpoint, tokenizer_snapshot=tokenizer_snapshot
    )
    if policy["unknown_reason"]:
        report_compile_issue("document_summary_capacity_unknown")
        logger.info("Document summary token capacity unknown: %s", policy["unknown_reason"])
        return lambda messages: True
    measure = TokenMeasure(policy)
    options = {**(generation_options or {}), "response_format": {"type": "json_object"}}
    return (
        lambda messages: measure.payload_tokens({"messages": messages, **options})
        <= measure.input_limit
    )


_SUMMARY_TASK = """Turn each supplied original section into a usable reference section.
For each operation or object, carry its applicability and prerequisites together
with its steps, parameters, alternatives, exceptions and results. Cover requirements
stated only once, including those without a planned concept page. Combine repetition
within the same scope; retain separate variants when their conditions differ.

Use explanatory prose for meaning, and verbatim fenced excerpts for command or
configuration syntax: preserve every token, prefix and comment marker in each quoted
line. A quoted template remains a template. When extraction has split a command or
lost table alignment, retain the readable fragment and identify that specific
structural gap; a reconstructed executable command or column assignment needs
explicit support. Reproduce reliably aligned tables with their object and condition
headers. Keep page/section boundaries and cross-references visible where they affect
interpretation. Preceding context supplies conditions, not extra primary ranges.

Include explanations, comparisons and source-supported calculations with their
premises. Each paragraph should explain or preserve the supplied section, without
adding unrelated advice or expanding a local observation to the entire document.
Return one JSON object in this complete shape:
{"description": "brief document description", "sections":
 [{"id": "the supplied output section id", "content": "complete Markdown knowledge"}]}.
Return every output_sections id exactly once, retaining its original requirements,
conditions and useful explanation in the corresponding body, not only its heading.
Use the document's necessary level of detail without a fixed compression ratio.
Return JSON without an outer code fence or frontmatter. Markdown code fences inside
section content are appropriate for exact technical excerpts.
"""


def summary_sections(content: str) -> list[dict]:
    """Group actual original headings or natural blocks, without generated navigation."""
    from openkb.content_blocks import markdown_layout

    tokens, offsets = markdown_layout(content)
    headings = {offsets[t.map[0]] for t in tokens if t.type == "heading_open" and t.map}
    # A parent heading immediately followed by a child heading is one section,
    # not an invitation to invent a body for the parent heading alone.
    body_starts = {
        offsets[t.map[0]]
        for t in tokens
        if t.map and t.level == 0 and t.type not in {"heading_open", "heading_close", "inline"}
    }
    boundaries = [0]
    for heading in sorted(headings):
        if any(boundaries[-1] <= start < heading for start in body_starts):
            boundaries.append(heading)
    boundaries.append(len(content))
    ends = sorted({offsets[t.map[1]] for t in tokens if t.map and t.level == 0})
    ranges = []
    for start, end in zip(boundaries, boundaries[1:]):
        if not content[start:end].strip():
            continue
        cursor = start
        for cut in ends:
            if cursor < cut < end and cut - cursor >= 4000:
                ranges.append((cursor, cut))
                cursor = cut
        if cursor < end:
            ranges.append((cursor, end))
    return [{"id": f"section-{i}", "start": a, "end": b} for i, (a, b) in enumerate(ranges, 1)]


def summary_messages(system: dict, doc_name: str, window: dict) -> list[dict]:
    window = {**window, "output_sections": summary_sections(window["content"])}
    return [
        system,
        {
            "role": "user",
            "content": f"Original document: {doc_name}\n" + json.dumps(window, ensure_ascii=False),
        },
        {"role": "user", "content": _SUMMARY_TASK},
    ]


@dataclass(frozen=True)
class SummaryDraft:
    markdown: str
    description: str
    planning_overview: str
    ranges: tuple[dict, ...]


async def build_document_summary(
    windows: CompileSourceWindows,
    *,
    system_message: dict,
    generate: Callable[[list[dict], str, list[str], tuple[str, ...]], Awaitable[dict]],
    fits: Callable[[list[dict]], bool],
    checkpoints=None,
) -> SummaryDraft:
    from openkb.agent.page_response import PageResponseError
    from openkb.llm_execution import raise_if_terminal

    try:
        batches = list(
            windows.iter_document_windows(
                fits=lambda window: fits(summary_messages(system_message, windows.doc_name, window))
            )
        )
    except (ValueError, OSError) as error:
        raise_if_terminal(error)
        if isinstance(error, PageResponseError):
            raise
        raise PageResponseError("document_summary_reader_invalid", str(error)) from error
    from openkb.compile_checkpoints import digest

    requests = [summary_messages(system_message, windows.doc_name, batch) for batch in batches]
    cached = [
        checkpoints.load(f"summary:{index}", digest(messages)) if checkpoints else None
        for index, messages in enumerate(requests, 1)
    ]
    if missing := sum(result is None for result in cached):
        admit_document_summary(missing)
    bodies, descriptions, ranges = [], [], []
    for index, batch in enumerate(batches, 1):
        from openkb.llm_execution import check_model_stop

        check_model_stop()
        messages = requests[index - 1]
        decoded = cached[index - 1]
        if decoded is None:
            if checkpoints:
                checkpoints.require_generation(f"summary:{index}", digest(messages))
            decoded = await generate(
                messages,
                f"document-summary:{index}",
                [batch["content"], *[c["content"] for c in batch["supplementary_context"]]],
                tuple(s["id"] for s in summary_sections(batch["content"])),
            )
            if checkpoints:
                checkpoints.save(
                    f"summary:{index}",
                    digest(messages),
                    decoded,
                    metadata={**batch, "output_sections": summary_sections(batch["content"])},
                )
        description = decoded["description"]
        body = "\n\n".join(section["content"] for section in decoded["sections"])
        bodies.append(body)
        descriptions.append(description)
        ranges.append(
            {
                key: batch[key]
                for key in ("path", "sha256", "selection_offset", "source_spans", "page_spans")
                if key in batch
            }
        )
    logger.info(
        "Document summary: %d original batches included for %s", len(bodies), windows.doc_name
    )
    if not batches:
        return SummaryDraft(
            f"# {windows.doc_name}\n\nThe retained original has no nonempty text.\n",
            "Retained original has no nonempty text.",
            "No nonempty text.",
            (),
        )
    if len(batches) == 1:
        return SummaryDraft(bodies[0], descriptions[0], descriptions[0], tuple(ranges))
    description = "\n".join(descriptions)
    markdown = f"# {windows.doc_name}\n\n" + "\n\n".join(bodies)
    return SummaryDraft(markdown, description, "\n".join(descriptions), tuple(ranges))
