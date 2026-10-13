"""Build ordered document knowledge from all original ranges, independently of page plans."""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openkb.agent.source_windows import CompileSourceWindows
from openkb.execution_budget import admit_document_summary
from openkb.source_context import COMPILATION_FACT_RULES

logger = logging.getLogger(__name__)
PROMPT_VERSION = "compiler-document-sections-v5"
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


def summary_sections(content: str, *, native_pages: bool = False) -> list[dict]:
    """Group actual original headings or natural blocks, without generated navigation."""
    from openkb.content_blocks import markdown_layout

    tokens, offsets = markdown_layout(content)
    headings = {
        offsets[t.map[0]] for t in tokens if not native_pages and t.type == "heading_open" and t.map
    }
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


def window_sections(window: dict) -> list[dict]:
    return summary_sections(window["content"], native_pages="page_spans" in window)


def summary_messages(system: dict, doc_name: str, window: dict) -> list[dict]:
    from openkb.agent.source_excerpts import SourceExcerpts

    excerpts = SourceExcerpts([window, *window["supplementary_context"]])
    window = {
        **window,
        "summary_protocol": PROMPT_VERSION,
        "output_sections": window_sections(window),
    }
    return excerpts.messages(
        [
            system,
            {
                "role": "user",
                "content": f"Original document: {doc_name}\n"
                + json.dumps(window, ensure_ascii=False, separators=(",", ":")),
            },
            {"role": "user", "content": _SUMMARY_TASK},
        ]
    )


@dataclass(frozen=True)
class SummaryDraft:
    markdown: str
    description: str
    planning_overview: str
    ranges: tuple[dict, ...]


@dataclass
class _SummaryRequest:
    key: str
    window: dict
    messages: list[dict]
    accepted: dict | None
    can_split: bool


async def build_document_summary(
    windows: CompileSourceWindows,
    *,
    system_message: dict,
    generate: Callable[[list[dict], str, list[dict], tuple[str, ...]], Awaitable[dict]],
    fits: Callable[[list[dict]], bool],
    checkpoints=None,
) -> SummaryDraft:
    from openkb.agent.page_response import PageResponseError, page_repair_message
    from openkb.llm_execution import raise_if_terminal

    def request_fits(window: dict, key: str) -> bool:
        messages = summary_messages(system_message, windows.doc_name, window)
        correction = page_repair_message(
            "page_truncated_response",
            f"LLM [document-summary:{key}] hit the length limit; "
            "skipping to avoid a truncated page",
            response_protocol="summary-json",
            expected_sections=tuple(s["id"] for s in window_sections(window)),
        )
        return fits(messages) and fits([*messages, correction])

    batches: list[dict] = []
    try:
        for window in windows.iter_document_windows(
            fits=lambda window: request_fits(window, str(len(batches) + 1))
        ):
            batches.append(window)
    except (ValueError, OSError) as error:
        raise_if_terminal(error)
        if isinstance(error, PageResponseError):
            raise
        raise PageResponseError("document_summary_reader_invalid", str(error)) from error
    from openkb.compile_checkpoints import digest

    def request(key: str, batch: dict, *, can_split: bool) -> _SummaryRequest:
        messages = summary_messages(system_message, windows.doc_name, batch)
        cached = checkpoints.load(f"summary:{key}", digest(messages)) if checkpoints else None
        return _SummaryRequest(key, batch, messages, cached, can_split)

    def children(parent: _SummaryRequest) -> list[_SummaryRequest]:
        divided = [
            request(f"{parent.key}.{i}", batch, can_split=False)
            for i, batch in enumerate(windows.split_document_window(parent.window), 1)
        ]
        if not all(request_fits(child.window, child.key) for child in divided):
            raise PageResponseError(
                "document_summary_capacity_exceeded", "Split request does not fit"
            )
        return divided

    requests = []
    for index, batch in enumerate(batches, 1):
        parent = request(str(index), batch, can_split=True)
        marker = (
            checkpoints.load(f"summary-split:{index}", digest(parent.messages))
            if checkpoints and parent.accepted is None
            else None
        )
        truncated = (
            checkpoints
            and parent.accepted is None
            and checkpoints.failure_code(f"summary:{index}", digest(parent.messages))
            == "page_truncated_response"
        )
        if marker is not None or truncated:
            divided = children(parent)
            child_digests = [digest(child.messages) for child in divided]
            if marker is not None and marker != child_digests:
                raise PageResponseError("compile_checkpoint_invalid", "Summary split changed")
            if marker is None:
                checkpoints.save(f"summary-split:{index}", digest(parent.messages), child_digests)
            requests.extend(divided)
        else:
            requests.append(parent)
    if missing := sum(r.accepted is None for r in requests):
        admit_document_summary(missing)
    bodies, descriptions, ranges = [], [], []
    cursor = 0
    while cursor < len(requests):
        from openkb.llm_execution import check_model_stop

        check_model_stop()
        current = requests[cursor]
        batch, messages = current.window, current.messages
        decoded = current.accepted
        if decoded is None:
            if checkpoints:
                checkpoints.claim_generation(
                    f"summary:{current.key}",
                    digest(messages),
                    stage=f"compile.document-summary:{current.key}",
                )
            try:
                decoded = await generate(
                    messages,
                    f"document-summary:{current.key}",
                    [batch, *batch["supplementary_context"]],
                    tuple(s["id"] for s in window_sections(batch)),
                )
            except PageResponseError as error:
                if error.code != "page_truncated_response" or not current.can_split:
                    raise
                divided = children(current)
                if checkpoints:
                    checkpoints.save(
                        f"summary-split:{current.key}",
                        digest(messages),
                        [digest(child.messages) for child in divided],
                    )
                requests[cursor : cursor + 1] = divided
                # The existing task ledger owns every send, including the failed
                # parent and each child's one repair. Reserve no fresh allowance.
                admit_document_summary(sum(r.accepted is None for r in requests[cursor:]))
                logger.info("Split truncated document summary batch %s once", current.key)
                continue
            if checkpoints:
                checkpoints.save(
                    f"summary:{current.key}",
                    digest(messages),
                    decoded,
                    metadata={**batch, "output_sections": window_sections(batch)},
                )
        else:
            from openkb.agent.page_response import parse_summary
            from openkb.agent.source_excerpts import SourceExcerpts

            sections = tuple(s["id"] for s in window_sections(batch))
            decoded = parse_summary(
                SourceExcerpts([batch, *batch["supplementary_context"]]).response(
                    json.dumps(decoded),
                    "summary-json",
                    sections,
                ),
                sections,
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
        cursor += 1
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
    if len(bodies) == 1:
        return SummaryDraft(bodies[0], descriptions[0], descriptions[0], tuple(ranges))
    description = "\n".join(descriptions)
    markdown = f"# {windows.doc_name}\n\n" + "\n\n".join(bodies)
    return SummaryDraft(markdown, description, "\n".join(descriptions), tuple(ranges))
