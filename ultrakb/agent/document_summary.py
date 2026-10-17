"""Build ordered document knowledge from all original ranges, independently of page plans."""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ultrakb.agent.compile_contract import (
    historical_summary_repairs,
    legacy_prompts,
    request_version,
    require_frozen_request,
)
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.agent.summary_protocol import (
    REFERENCED_SUMMARY,
    SECTIONED_SUMMARY,
    summary_response_format,
)
from ultrakb.execution_budget import admit_document_summary
from ultrakb.source_context import COMPILATION_FACT_RULES

logger = logging.getLogger(__name__)
PROMPT_VERSION = "compiler-document-sections-v5"
REFERENCE_PROMPT_VERSION = "compiler-document-references-v1"
_SPLITTABLE_DELIVERY_ERRORS = {
    "page_truncated_response",
    "summary_invalid_sections",
    "summary_missing_sections",
}
SYSTEM_TEMPLATE = (
    "You are UltraKB's wiki compilation agent for document knowledge. "
    "Write independently readable knowledge in {language}.\n" + COMPILATION_FACT_RULES
)


def summary_capacity(
    config: dict,
    *,
    custom_endpoint: bool,
    tokenizer_snapshot=None,
    generation_options: dict | None = None,
    legacy_format: bool = False,
    base_url: str | None = None,
) -> Callable[[list[dict]], bool]:
    from ultrakb.compilation_report import report_compile_issue
    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.model_tokenizer import TokenMeasure

    policy = capacity_policy(
        config,
        custom_endpoint=custom_endpoint,
        tokenizer_snapshot=tokenizer_snapshot,
        base_url=base_url,
    )
    if policy["unknown_reason"]:
        report_compile_issue("document_summary_capacity_unknown")
        from ultrakb.model_tokenizer import ContextCapacityError

        raise ContextCapacityError("Document summary capacity unknown: " + policy["unknown_reason"])
    measure = TokenMeasure(policy)
    from ultrakb.agent.compile_capacity import CompileCapacity
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, RoleBindings, active_executor

    executor = active_executor() or CompletionExecutor(
        RoleBindings(
            config["model"],
            LlmCredentialBundle(
                base_url=base_url or ("https://custom.invalid" if custom_endpoint else None)
            ),
        )
    )
    sizing = CompileCapacity(executor, measure)

    def fits(messages):
        sections = ()
        protocol = summary_request_protocol(messages)
        excerpt_ids = ()
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and content.startswith("Original document: "):
                original = json.loads(content.split("\n", 1)[1])
                sections = tuple(section["id"] for section in original["output_sections"])
            if isinstance(content, str) and content.startswith(
                "Structured original reference catalog:\n"
            ):
                excerpt_ids = tuple(
                    r["id"] for r in json.loads(content.split("\n", 1)[1])["references"]
                )
        response_format = summary_response_format(
            protocol,
            sections,
            model=config["model"],
            base_url=base_url or ("https://custom.invalid" if custom_endpoint else None),
            legacy=legacy_format,
            excerpt_ids=excerpt_ids,
        )
        options = {**(generation_options or {}), "response_format": response_format}
        return sizing(messages, options)

    return fits


_SUMMARY_TASK = (
    "Turn each supplied original section into a usable reference section. Preserve its "
    "objects, applicability, prerequisites, steps, parameters, alternatives, exceptions, "
    "results and explanations. Keep variants and source positions distinct. Generated "
    "Markdown may include inline code, fences, tables, examples and supported calculations "
    "with their premises. Use explicit excerpt IDs when an exact original copy is needed; "
    "the program copies and attributes it. Reading coverage proves protocol completion, "
    "not factual correctness or semantic completeness. "
    "Return one JSON object in this complete shape: " + SECTIONED_SUMMARY.example() + ". "
    "Return every output_sections ID exactly once with useful knowledge, not just headings. "
    "Use the necessary detail, preserving source conditions and local evidence gaps. "
    "Place [[original:ID]] alone in an unindented top-level paragraph with blank lines."
)


def summary_sections(content: str, *, native_pages: bool = False) -> list[dict]:
    """Group actual original headings or natural blocks, without generated navigation."""
    from ultrakb.content_blocks import markdown_layout

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


def summary_request_protocol(messages: list[dict]) -> str:
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content.startswith("Original document: "):
            if (
                json.loads(content.split("\n", 1)[1]).get("summary_protocol")
                == REFERENCE_PROMPT_VERSION
            ):
                return "summary-parts-json"
    return "summary-json"


def summary_messages(
    system: dict, doc_name: str, window: dict, *, structured=False, request_format=None
) -> list[dict]:
    from ultrakb.agent.source_excerpts import SourceExcerpts

    version = request_version() if request_format is None else request_format
    excerpts = SourceExcerpts([window, *window["supplementary_context"]], request_format=version)
    window = {
        **window,
        "summary_protocol": REFERENCE_PROMPT_VERSION if structured else PROMPT_VERSION,
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
            {
                "role": "user",
                "content": (
                    legacy_prompts()["summary_reference_task" if structured else "summary_task"]
                    if version == 1
                    else (
                        _SUMMARY_TASK.split("Return one JSON object in this complete shape:")[0]
                        + (
                            "Return one JSON object with description and sections with every "
                            "requested ID exactly once. "
                            if version >= 3
                            else (
                                "Return description and sections with every requested ID "
                                "exactly once. "
                            )
                        )
                        + "Each section has ordered parts with kind, text and refs. Prose uses "
                        "ordinary Markdown and refs: []; excerpt uses empty text and "
                        "supplied refs. "
                        "Legacy derived_template uses nonempty text and resolvable refs. "
                        + REFERENCED_SUMMARY.example()
                        if structured
                        else _SUMMARY_TASK
                    )
                ),
            },
        ],
        structured=structured,
    )


@dataclass(frozen=True)
class SummaryDraft:
    markdown: str
    description: str
    planning_overview: str
    ranges: tuple[dict, ...]


def planning_overview(draft: SummaryDraft, max_chars: int) -> str:
    """Bound planning context while explicitly identifying a partial description."""
    overview = draft.planning_overview[:max_chars]
    if len(draft.planning_overview) > len(overview):
        overview += (
            "\nDescriptions are a partial prefix; the document knowledge covers all batches."
        )
    return overview


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
    max_concurrency: int = 1,
) -> SummaryDraft:
    from ultrakb.agent.page_response import PageResponseError, summary_repair_messages
    from ultrakb.llm_execution import raise_if_terminal

    structured = getattr(checkpoints, "structured_summary", False) if checkpoints else True
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= 3:
        raise ValueError("Document summary concurrency must be 1, 2 or 3")
    from ultrakb.llm_execution import active_executor

    if executor := active_executor():
        max_concurrency = min(max_concurrency, executor.policy.concurrency)
    if prepare := getattr(checkpoints, "prepare_summary_delivery", None):
        prepare()

    def request_fits(window: dict) -> bool:
        messages = summary_messages(
            system_message,
            windows.doc_name,
            window,
            structured=structured,
            request_format=request_version(checkpoints),
        )
        corrections = summary_repair_messages(
            tuple(section["id"] for section in window_sections(window)),
            protocol=summary_request_protocol(messages),
        )
        if request_version(checkpoints) == 1:
            corrections = historical_summary_repairs(
                summary_request_protocol(messages),
                tuple(section["id"] for section in window_sections(window)),
            )
        return fits(messages) and all(fits([*messages, correction]) for correction in corrections)

    batches: list[dict] = []
    try:
        for window in windows.iter_document_windows(fits=request_fits):
            if window["content"].strip():
                batches.append(window)
    except (ValueError, OSError) as error:
        raise_if_terminal(error)
        if isinstance(error, PageResponseError):
            raise
        raise PageResponseError("document_summary_reader_invalid", str(error)) from error
    from ultrakb.compile_checkpoints import digest

    def request(key: str, batch: dict, *, can_split: bool) -> _SummaryRequest:
        messages = summary_messages(
            system_message,
            windows.doc_name,
            batch,
            structured=structured,
            request_format=request_version(checkpoints),
        )
        require_frozen_request(checkpoints, f"summary:{key}", digest(messages))
        cached = checkpoints.load(f"summary:{key}", digest(messages)) if checkpoints else None
        if cached is not None:
            from ultrakb.agent.page_delivery import validate_page_delivery
            from ultrakb.agent.source_excerpts import SourceExcerpts

            validate_page_delivery(
                json.dumps(cached),
                response_protocol="summary-json",
                expected_sections=tuple(s["id"] for s in window_sections(batch)),
                original_excerpts=SourceExcerpts([batch, *batch["supplementary_context"]]),
            )
        if checkpoints and cached is None:
            from ultrakb.agent.retained_summary import reuse_saved_summary

            cached = reuse_saved_summary(
                checkpoints,
                f"summary:{key}",
                digest(messages),
                batch,
                protocol=summary_request_protocol(messages),
                section_ranges=window_sections(batch),
            )
        return _SummaryRequest(key, batch, messages, cached, can_split)

    def children(parent: _SummaryRequest) -> list[_SummaryRequest]:
        parts = list(windows.split_document_window(parent.window))
        if len(parts) < 2 or any(
            len(part["content"]) >= len(parent.window["content"]) for part in parts
        ):
            raise PageResponseError(
                "document_summary_capacity_exceeded", "Minimal complete source cannot be split"
            )
        divided = [
            request(f"{parent.key}.{i}", batch, can_split=True) for i, batch in enumerate(parts, 1)
        ]
        if not all(request_fits(child.window) for child in divided):
            raise PageResponseError(
                "document_summary_capacity_exceeded", "Split request does not fit"
            )
        return divided

    split_parents: set[str] = set()

    def restored(parent):
        marker = (
            checkpoints.load(f"summary-split:{parent.key}", digest(parent.messages))
            if checkpoints and parent.accepted is None
            else None
        )
        truncated = (
            checkpoints
            and parent.accepted is None
            and checkpoints.failure_code(f"summary:{parent.key}", digest(parent.messages))
            in _SPLITTABLE_DELIVERY_ERRORS
        )
        if marker is not None or truncated:
            split_parents.add(parent.key)
            divided = children(parent)
            child_digests = [digest(child.messages) for child in divided]
            if marker is not None and marker != child_digests:
                raise PageResponseError("compile_checkpoint_invalid", "Summary split changed")
            if marker is None:
                checkpoints.save(
                    f"summary-split:{parent.key}", digest(parent.messages), child_digests
                )
            return [child for part in divided for child in restored(part)]
        return [parent]

    requests = [
        child
        for index, batch in enumerate(batches, 1)
        for child in restored(request(str(index), batch, can_split=True))
    ]
    if missing := sum(r.accepted is None for r in requests):
        admit_document_summary(missing)

    async def complete(current):
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
                if error.code not in _SPLITTABLE_DELIVERY_ERRORS or not current.can_split:
                    raise
                divided = children(current)
                if checkpoints:
                    checkpoints.save(
                        f"summary-split:{current.key}",
                        digest(messages),
                        [digest(child.messages) for child in divided],
                    )
                logger.info("Split document summary batch %s after %s", current.key, error.code)
                return divided
        if decoded is None:
            raise PageResponseError("summary_response_invalid", "Summary delivery is not JSON")
        retained = decoded
        if current.accepted is not None or any(
            "parts" in section for section in decoded["sections"]
        ):
            from ultrakb.agent.page_response import parse_summary
            from ultrakb.agent.source_excerpts import SourceExcerpts

            sections = tuple(s["id"] for s in window_sections(batch))
            decoded = parse_summary(
                SourceExcerpts([batch, *batch["supplementary_context"]]).response(
                    json.dumps(decoded),
                    "summary-json",
                    sections,
                ),
                sections,
            )
        if checkpoints and current.accepted is None:
            checkpoints.save(
                f"summary:{current.key}",
                digest(messages),
                retained,
                metadata={**batch, "output_sections": window_sections(batch)},
            )
        return decoded

    pending = deque(requests)
    running: dict[asyncio.Task, _SummaryRequest] = {}
    accepted: dict[str, tuple[_SummaryRequest, dict]] = {}
    leaves = {request.key for request in requests}
    from ultrakb.compilation_report import compile_repair_count

    initial_repairs = compile_repair_count()

    def notify_work():
        from ultrakb.compilation_report import report_compile_work

        report_compile_work(
            "summary",
            completed=len(accepted),
            pending=len(leaves) - len(accepted) - len(running),
            in_flight=len(running),
            splits=len(split_parents),
            repairs=compile_repair_count() - initial_repairs,
        )

    failure: BaseException | None = None
    notify_work()
    try:
        while pending or running:
            from ultrakb.llm_execution import check_model_stop

            while pending and len(running) < max_concurrency and failure is None:
                try:
                    check_model_stop()
                except BaseException as error:
                    failure = error
                    break
                current = pending.popleft()
                running[asyncio.create_task(complete(current))] = current
                notify_work()
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                current = running.pop(task)
                try:
                    result = task.result()
                    if isinstance(result, list):
                        split_parents.add(current.key)
                        leaves.remove(current.key)
                        leaves.update(child.key for child in result)
                        pending.extendleft(reversed(result))
                        if failure is None:
                            # In-flight sends are already charged to the shared budget.
                            admit_document_summary(sum(r.accepted is None for r in pending))
                    else:
                        accepted[current.key] = current, result
                except BaseException as error:
                    failure = failure or error
                notify_work()
            if failure is not None:
                pending.clear()
        if failure is not None:
            raise failure
    finally:
        # Do not cancel in-flight transports or detach their paid responses from
        # the KB lease. Each worker settles its receipt/checkpoint before return.
        if running:
            await asyncio.shield(asyncio.gather(*running, return_exceptions=True))

    bodies, descriptions, ranges = [], [], []
    for key in sorted(accepted, key=lambda value: tuple(map(int, value.split(".")))):
        current, decoded = accepted[key]
        batch = current.window
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
    if len(bodies) == 1:
        return SummaryDraft(bodies[0], descriptions[0], descriptions[0], tuple(ranges))
    description = "\n".join(descriptions)
    markdown = f"# {windows.doc_name}\n\n" + "\n\n".join(bodies)
    return SummaryDraft(markdown, description, "\n".join(descriptions), tuple(ranges))
