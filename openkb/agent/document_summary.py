"""Build ordered document knowledge from all original ranges, independently of page plans."""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openkb.agent.page_response import page_fields
from openkb.agent.source_windows import CompileSourceWindows
from openkb.execution_budget import admit_document_summary
from openkb.source_context import COMPILATION_FACT_RULES

logger = logging.getLogger(__name__)
PROMPT_VERSION = "compiler-document-knowledge-v1"
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


_SUMMARY_TASK = """Write independently readable knowledge from this original batch.
Retain everything that affects understanding, decisions or operation: objects,
requirements, steps, prerequisites, exceptions, values and their scope. Include
useful explanations, comparisons, verifiable calculations or examples supported
by these originals, with their premises. A requirement stated only once still
belongs in this document summary even if no concept or entity page will cover it.
Combine repetition while retaining conditions that change the conclusion. Keep
original headings and native locations distinct from generated navigation labels.
Preceding context supplies conditions, not extra primary ranges. Preserve unresolved
cross-references and qualifications; this batch cannot establish what the whole
document omits. Return one JSON object with a brief string description and nonempty
Markdown content. Use the document's necessary level of detail, without a fixed
item count or compression ratio. Return no frontmatter or code fences.
"""


def summary_messages(system: dict, doc_name: str, window: dict) -> list[dict]:
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
    generate: Callable[[list[dict], str, list[str]], Awaitable[str]],
    fits: Callable[[list[dict]], bool],
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
    admit_document_summary(len(batches))
    bodies, descriptions, ranges = [], [], []
    for index, batch in enumerate(batches, 1):
        messages = summary_messages(system_message, windows.doc_name, batch)
        raw = await generate(
            messages,
            f"document-summary:{index}",
            [batch["content"], *[context["content"] for context in batch["supplementary_context"]]],
        )
        description, body, _ = page_fields(raw)
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
    return SummaryDraft(
        "\n\n".join(bodies), descriptions[0], "\n".join(descriptions), tuple(ranges)
    )
