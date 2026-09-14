"""Q&A agent for querying the OpenKB knowledge base."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any

from agents import Agent, Runner, function_tool

from openkb.agent.answer_references import resolve_references
from openkb.agent.query_prompt import QUERY_INSTRUCTIONS_TEMPLATE
from openkb.agent.streaming import settled_stream
from openkb.agent.tools import (
    artifact_event_from_write,
    get_wiki_page_content,
    read_wiki_file,
    write_kb_file,
)
from openkb.config import LlmCredentialBundle, resolve_model_settings
from openkb.schema import get_agents_md

MAX_TURNS = 50


def build_query_agent(
    wiki_root: str,
    model: str,
    language: str = "en",
    bundle: "LlmCredentialBundle | None" = None,
) -> Agent:
    """Build and return the Q&A agent."""
    schema_md = get_agents_md(Path(wiki_root))
    instructions = QUERY_INSTRUCTIONS_TEMPLATE.format(schema_md=schema_md)
    instructions += f"\n\nIMPORTANT: Answer in {language} language."

    @function_tool
    def read_file(path: str, offset: int = 0, max_chars: int = 16000) -> str:
        """Read a bounded Markdown window. Follow next_offset when returned.
        Args:
            path: File path relative to wiki root (e.g. 'summaries/paper.md').
            offset: Character offset, starting at zero.
            max_chars: Maximum window size, from 1 to 16000 characters.
        """
        if (
            type(offset) is not int
            or offset < 0
            or type(max_chars) is not int
            or not 1 <= max_chars <= 16000
        ):
            raise ValueError("Invalid wiki reading window")
        text = read_wiki_file(path, wiki_root)
        if offset == 0 and len(text) <= max_chars:
            return text
        end = min(offset + max_chars, len(text))
        return json.dumps(
            {
                "path": path,
                "offset": offset,
                "text": text[offset:end],
                "total_chars": len(text),
                "next_offset": end if end < len(text) else None,
            },
            ensure_ascii=False,
        )

    @function_tool
    def get_page_content(doc_name: str, pages: str) -> str:
        """Get text content of specific pages from a PageIndex (long) document.
        Only use for documents with doc_type: pageindex. For short documents,
        use read_file instead.
        Args:
            doc_name: Document name (e.g. 'attention-is-all-you-need').
            pages: Page specification (e.g. '3-5,7,10-12').
        """
        return get_wiki_page_content(doc_name, pages, wiki_root)

    from openkb.agent.source_tools import source_tools
    from openkb.vision.session import image_tools

    original_tools, original_instructions = source_tools(Path(wiki_root).parent)
    instructions += "\n\n" + original_instructions

    visual_tools, visual_instructions = image_tools(Path(wiki_root).parent)
    instructions += "\n\n" + visual_instructions

    from agents.model_settings import ModelSettings

    if bundle is not None:
        model_settings: dict[str, Any] = {
            "parallel_tool_calls": (
                bundle.parallel_tool_calls if bundle.parallel_tool_calls_explicit else False
            ),
            "extra_headers": bundle.extra_headers or None,
            "extra_args": {"timeout": bundle.timeout} if bundle.timeout is not None else None,
        }
    else:
        model_settings = resolve_model_settings()

    model_settings["include_usage"] = True

    from openkb.agent.completion_model import CompletionAwareModel
    from openkb.processing import request_budget_settings

    if caps := request_budget_settings():
        model_settings["max_tokens"] = caps["max_tokens"]
        model_settings["extra_args"] = {
            **(model_settings.get("extra_args") or {}),
            "timeout": caps["timeout"],
        }
    from openkb.agent.answer_review import SourceAnswerAgent
    from openkb.config import compilation_model_options, resolve_effective_config

    review_options = compilation_model_options(
        resolve_effective_config(Path(wiki_root).parent)[0], verification=True
    )
    settings = ModelSettings(**model_settings)
    return SourceAnswerAgent(
        name="wiki-query",
        instructions=instructions,
        tools=[read_file, get_page_content, *original_tools, *visual_tools],
        model=CompletionAwareModel(model=model),
        model_settings=settings,
        image_understanding_enabled=bool(visual_tools),
        answer_review_settings=replace(
            settings, extra_args={**(settings.extra_args or {}), **review_options}
        ),
    )


def _resolve_tool_call_id(raw_item: Any) -> str | None:
    """Resolve a tool call's correlation id exactly as the Agents SDK's
    ``ToolCallItem.call_id`` / ``ToolCallOutputItem.call_id`` property does:
    prefer ``call_id``, fall back to ``id``, dict-aware.

    The ChatCompletions/LiteLLM path emits only ``id`` (no ``call_id``) on the
    output item, so without the ``id`` fallback the key written on the
    ``tool_call`` side and the key read on the ``tool_call_output_item`` side
    disagree, ``pending_calls.pop`` misses, and the ``output/*.html`` artifact
    card silently never fires. Deriving the key with this one helper on BOTH
    sides keeps them aligned.
    """
    if isinstance(raw_item, dict):
        return raw_item.get("call_id") or raw_item.get("id")
    return getattr(raw_item, "call_id", None) or getattr(raw_item, "id", None)


async def iter_agent_response_events(
    agent: Agent,
    input_data: str | list[dict[str, Any]],
    *,
    max_turns: int = MAX_TURNS,
    run_config: Any = None,
    _replacement_attempts: int = 1,
    _citation_attempts: int = 1,
    _evidence_attempts: int = 1,
    _rejected_answer: str | None = None,
    _answer_correction: Any = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield non-TTY events for a streamed agent response.

    The CLI renders these events to stdout; the REST API serializes the same
    events as SSE. Events: ``{"event": "delta", "data": {"text": ...}}`` for
    each response-text delta, ``{"event": "tool_call", "data": {...}}`` for
    tool invocations, and a final ``{"event": "final", "data": {"answer": ...,
    "history": [...]}}`` carrying the complete answer and reusable Agents SDK
    history.
    """
    from agents import RawResponsesStreamEvent, RunItemStreamEvent
    from openai.types.responses import ResponseTextDeltaEvent

    from openkb.agent.request_budget import RequestBudgetHooks
    from openkb.vision.history import text_history

    hooks = RequestBudgetHooks()
    original_input = input_data
    input_data = text_history(input_data)
    result = (
        Runner.run_streamed(
            agent, input_data, max_turns=max_turns, run_config=run_config, hooks=hooks
        )
        if run_config
        else Runner.run_streamed(agent, input_data, max_turns=max_turns, hooks=hooks)
    )
    pending_calls: dict[str, tuple[str, str]] = {}

    stream = settled_stream(result)
    try:
        async for event in stream:
            if isinstance(event, RawResponsesStreamEvent):
                if isinstance(event.data, ResponseTextDeltaEvent):
                    text = event.data.delta
                    if text and _answer_correction is None:
                        yield {"event": "delta", "data": {"text": text}}
            elif isinstance(event, RunItemStreamEvent):
                item = event.item
                if item.type == "tool_call_item":
                    raw_item = item.raw_item
                    name = getattr(raw_item, "name", "?")
                    arguments = getattr(raw_item, "arguments", "") or ""
                    call_id = _resolve_tool_call_id(raw_item)
                    if call_id:
                        pending_calls[call_id] = (name, arguments)
                    yield {"event": "tool_call", "data": {"name": name, "arguments": arguments}}
                elif item.type == "tool_call_output_item":
                    raw_item = item.raw_item
                    call_id = _resolve_tool_call_id(raw_item)
                    name, arguments = (
                        pending_calls.pop(call_id, ("", ""))
                        if isinstance(call_id, str)
                        else ("", "")
                    )
                    payload = artifact_event_from_write(
                        name, arguments, str(getattr(item, "output", "") or "")
                    )
                    if payload is not None:
                        yield {"event": "artifact", "data": payload}

    finally:
        try:
            await stream.aclose()
        finally:
            hooks.close()

    from openkb.agent.answer_text import visible_answer
    from openkb.processing import processing_checkpoint

    processing_checkpoint()
    from openkb.agent.answer_citations import invalid_source_targets
    from openkb.agent.completion_model import answer_truncated

    if _answer_correction is not None:
        from openkb.agent.answer_result import RenderedAnswer
        from openkb.processing import OutputTruncated

        if answer_truncated(result):
            raise OutputTruncated("answering")
        result = RenderedAnswer(result, _answer_correction.apply(result.final_output))
    result = resolve_references(result)
    truncated = answer_truncated(result)
    empty = isinstance(result.final_output, str) and not visible_answer(result.final_output).strip()
    invalid_targets = [] if truncated or empty else invalid_source_targets(result)
    from openkb.agent.answer_review import review_answer
    from openkb.processing import OutputTruncated, ProcessingIncomplete

    if (
        _rejected_answer is not None
        and isinstance(result.final_output, str)
        and (" ".join(result.final_output.split()) == " ".join(_rejected_answer.split()))
    ):
        raise ProcessingIncomplete("answer_evidence_unsupported", "answering")
    issues = []
    if not truncated and not empty and not invalid_targets:
        try:
            issues = await review_answer(agent, result, run_config=run_config)
        except ProcessingIncomplete as exc:
            if exc.reason != "answer_verification_invalid":
                raise
            if not _evidence_attempts:
                raise
            # An invalid review has no authorized edit scope. Retry that protocol
            # once with the exact unchanged answer, consuming the same allowance.
            _evidence_attempts -= 1
            issues = await review_answer(agent, result, run_config=run_config)
    evidence_problem = bool(issues)
    if truncated or empty or invalid_targets or evidence_problem:
        allowance = (
            _evidence_attempts
            if evidence_problem
            else _citation_attempts
            if invalid_targets
            else _replacement_attempts
        )
        if not allowance:
            if truncated:
                raise OutputTruncated("answering")
            if empty:
                raise ProcessingIncomplete("answer_empty", "answering")
            raise ProcessingIncomplete(
                "answer_evidence_unsupported" if issues else "answer_citation_invalid", "answering"
            )
        history = [item for item in result.to_input_list() if item.get("status") != "incomplete"]
        if (
            (empty or invalid_targets or evidence_problem)
            and history
            and history[-1].get("role") == "assistant"
        ):
            history.pop()  # Do not persist the rejected draft in a completed conversation.
        reason = (
            "The previous response hit its output limit. "
            if truncated
            else "The previous response contained no answer text. "
            if empty
            else "The previous response failed evidence review. "
            + (
                json.dumps(issues, ensure_ascii=False)
                if issues
                else "Source citation targets were absent from tool evidence. "
            )
        )
        correction = _answer_correction
        if correction is not None:
            # Citation or completion recovery after a semantic patch must retain
            # the ORIGINAL permitted units; it cannot reopen whole-answer edits.
            instruction = correction.request(
                issues
                or [
                    {"kind": "citation", "claim": target, "reason": "Unobserved citation"}
                    for target in invalid_targets
                ],
                previous=result.final_output,
            )
        elif evidence_problem:
            from openkb.agent.answer_correction import AnswerCorrection

            correction = AnswerCorrection(result.final_output, issues)
            instruction = correction.request(issues)
        else:
            instruction = (
                reason + "Produce one concise, complete replacement answer to the original "
                "question using the evidence already read. Include only requested fields, "
                "omit optional explanations, and finish all source citations. Copy observed "
                "short_citation markers or exact source targets from tool results; never "
                "invent IDs, paths or anchors. Do not repeat the search or invent support."
            )
        history.append({"role": "developer", "content": instruction})
        recovery_position = len(history) - 1
        replacement_stream = iter_agent_response_events(
            agent.clone(
                tools=[],
                handoffs=[],
                model_settings=replace(agent.model_settings, tool_choice="none"),
            ),
            history,
            max_turns=1,
            run_config=run_config,
            _replacement_attempts=_replacement_attempts - int(truncated or empty),
            _citation_attempts=_citation_attempts - int(bool(invalid_targets)),
            _evidence_attempts=_evidence_attempts - int(evidence_problem),
            _rejected_answer=result.final_output if issues else _rejected_answer,
            _answer_correction=correction,
        )
        async with aclosing(replacement_stream):
            async for event in replacement_stream:
                if event["event"] == "final":
                    # This instruction belongs to one repair request, never the next
                    # user question. Preserve any pre-existing developer messages.
                    completed = event["data"]["history"]
                    event["data"]["history"] = (
                        completed[:recovery_position] + completed[recovery_position + 1 :]
                    )
                yield event
        return
    # Deltas also contain assistant narration before tool calls. The SDK's
    # terminal output identifies the actual answer, independently of that trace.
    final = result.final_output
    if not isinstance(final, str):
        raise RuntimeError("The model did not return a final text answer")
    answer = visible_answer(final)
    yield {
        "event": "final",
        "data": {
            "answer": answer,
            "history": (
                original_input + result.to_input_list()[len(original_input) :]
                if isinstance(original_input, list)
                else result.to_input_list()
            ),
        },
    }


def build_chat_agent(
    kb_dir: Path,
    model: str,
    language: str = "en",
    bundle: "LlmCredentialBundle | None" = None,
) -> Agent:
    """Build the chat agent: query agent + a write tool restricted to
    ``<kb>/wiki/explorations/**`` and ``<kb>/output/**`` + a ``ShellTool``
    advertising locally-installed Anthropic-style skills.

    This is the variant used by the interactive ``openkb chat`` REPL so users
    can iterate on generated artifacts (e.g. ``output/skills/<name>/``) via
    natural-language follow-ups without giving the agent unrestricted write
    access to the wiki.

    Skill discovery: ``openkb/agent/skills.scan_local_skills`` looks in
    ``<kb>/skills/``, ``~/.openkb/skills/``, ``~/.claude/skills/`` for
    ``SKILL.md`` files. Any found skill is exposed to the agent via
    ``ShellTool.environment.skills`` so the model can ``cat`` the skill body
    and follow its instructions when the user's request matches.
    """
    wiki_root = str(kb_dir / "wiki")
    kb_root = str(kb_dir)
    base = build_query_agent(wiki_root, model, language=language, bundle=bundle)

    @function_tool
    def write_file(path: str, content: str) -> str:
        """Write a text file under the KB.

        Allowed paths (relative to KB root):
          * ``wiki/explorations/**`` — chat-derived notes.
          * ``output/**``            — generator artifacts (skills, etc.).

        Any other path is rejected. Parent directories are created.

        Args:
            path: File path relative to KB root
                (e.g. ``"output/skills/demo/SKILL.md"``).
            content: Full text content to write (overwrites if file exists).
        """
        return write_kb_file(path, content, kb_root)

    extra_tools: list = [write_file]
    skill_instructions_addendum = ""

    # Skill discovery via function tools. The agents SDK has a richer
    # ``ShellTool``+``ShellToolLocalSkill`` mechanism for this, but those
    # are OpenAI Responses-API hosted tools; LiteLLM routes through
    # ChatCompletions which rejects hosted tools. So we use plain
    # ``function_tool`` primitives that work with any LiteLLM-routed model.
    from openkb.agent.skills import scan_local_skills

    skills = scan_local_skills(kb_dir)
    skill_index = {s["name"]: s for s in skills}

    if skill_index:
        skill_list_text = _format_skill_list(skills)

        @function_tool
        def list_skills() -> str:
            """List skills available in this environment.

            Returns a text catalog of installed Anthropic-style skills.
            Each entry has a name and a one-line description; use the
            description to decide whether the skill matches the user's
            request, then call ``read_skill(name)`` to load its body.
            """
            return skill_list_text

        @function_tool
        def read_skill(name: str) -> str:
            """Read a skill's ``SKILL.md`` body.

            Call this once you've decided a skill matches the user's
            request. The returned text is the full skill instructions
            (frontmatter stripped). Follow it as your working method
            and write outputs via the ``write_file`` tool.

            Args:
                name: skill name as listed by ``list_skills``.
            """
            entry = skill_index.get(name)
            if entry is None:
                return f"Unknown skill: {name!r}. Call list_skills() to see available skills."
            md_path = Path(entry["path"]) / "SKILL.md"
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError as exc:
                return f"Could not read {md_path}: {exc}"
            # Strip frontmatter, return body only.
            from openkb.agent.skills import _parse_frontmatter

            _, body = _parse_frontmatter(text)
            return body

        extra_tools.extend([list_skills, read_skill])

        # Build the prompt addendum listing skill names + descriptions
        # right inside the system prompt so the model sees them up front
        # and knows what to look for, even before deciding to call
        # list_skills(). This is the difference between "agent
        # eventually discovers skills" and "agent treats skill use as
        # the default for matching requests".
        skill_lines = []
        for s in skills:
            desc_one_line = " ".join(s["description"].split())
            skill_lines.append(f"- **{s['name']}** — {desc_one_line}")
        skill_instructions_addendum = (
            "\n\n## Available skills\n\n"
            "The following Anthropic-style skill packages are installed in "
            "this environment. **When a user request matches a skill's "
            "description (e.g. 'make a deck', 'generate slides', 'draft a "
            "report'), you MUST call `read_skill(name)` to load that "
            "skill's full instructions and follow them strictly** — do not "
            "freestyle the output format if a skill covers it.\n\n"
            + "\n".join(skill_lines)
            + "\n\nIf no listed skill matches the request, proceed with "
            "your default tools."
        )

    new_instructions = (base.instructions or "") + skill_instructions_addendum
    return base.clone(
        tools=[*base.tools, *extra_tools],
        instructions=new_instructions,
    )


def _format_skill_list(skills: list[dict[str, str]]) -> str:
    """Render the skill catalog as a compact text block for the agent."""
    if not skills:
        return "No skills installed."
    lines = [f"{len(skills)} skill(s) available:\n"]
    for s in skills:
        lines.append(f"- {s['name']}")
        # Indent description; keep it one paragraph so the agent reads it fast.
        desc = " ".join(s["description"].split())
        lines.append(f"    {desc}")
    lines.append("\nTo use a skill, call read_skill(name) and follow its instructions.")
    return "\n".join(lines)


async def run_query(
    question: str,
    kb_dir: Path,
    model: str,
    stream: bool = False,
    *,
    raw: bool = False,
    run_config: Any = None,
    bundle: LlmCredentialBundle | None = None,
) -> str:
    """Run the terminal query and its review within one configured task allowance."""
    from openkb.agent.request_budget import visual_task_budget
    from openkb.processing import processing_checkpoint

    with visual_task_budget(kb_dir):
        answer = await _run_query(
            question, kb_dir, model, stream, raw=raw, run_config=run_config, bundle=bundle
        )
        processing_checkpoint()
        return answer


async def _run_query(
    question: str,
    kb_dir: Path,
    model: str,
    stream: bool = False,
    *,
    raw: bool = False,
    run_config: Any = None,
    bundle: LlmCredentialBundle | None = None,
) -> str:
    """Run a Q&A query against the knowledge base.

    Args:
        question: The user's question.
        kb_dir: Root of the knowledge base.
        model: LLM model name.
        stream: If True, show tool progress and print the verified answer.
        raw: If True, write raw markdown source instead of rendering it
            (still keeps tool-call line styling).

    Returns:
        The agent's final answer as a string.
    """
    import sys

    from openkb.config import resolve_effective_config

    config = (await asyncio.to_thread(resolve_effective_config, kb_dir))[0]
    language: str = config.get("language", "en")

    wiki_root = str(kb_dir / "wiki")

    agent = build_query_agent(wiki_root, model, language=language, bundle=bundle)
    from openkb.agent.request_budget import RequestBudgetHooks

    if not stream:
        hooks = RequestBudgetHooks()
        try:
            result = await Runner.run(
                agent, question, max_turns=MAX_TURNS, run_config=run_config, hooks=hooks
            )
        finally:
            hooks.close()
        from openkb.agent.completion_model import answer_truncated
        from openkb.processing import OutputTruncated

        if answer_truncated(result):
            raise OutputTruncated("answering")
        from openkb.agent.answer_citations import require_source_targets

        result = resolve_references(result)
        require_source_targets(result)
        from openkb.agent.answer_review import require_supported_answer

        await require_supported_answer(agent, result, run_config=run_config)
        return result.final_output or ""

    import os

    from openkb.agent.chat import _build_style
    from openkb.agent.terminal_answer import terminal_answer

    use_color = bool(sys.stdout.isatty() and not os.environ.get("NO_COLOR", ""))
    answer, _ = await terminal_answer(
        agent,
        question,
        _build_style(use_color),
        use_color=use_color,
        raw=raw,
        run_config=run_config,
    )
    return answer


def build_run_config_from_bundle(model: str, bundle: "LlmCredentialBundle | None") -> Any:
    """Build an Agents-SDK `RunConfig` from a credential bundle.

    When *bundle* is `None` (CLI path), returns `None` so the runner falls
    back to the default provider (process-wide `litellm.api_key` / env vars).
    When a bundle is supplied, a dedicated `LitellmModel` instance is created
    with the per-KB `api_key` and `base_url` so concurrent requests on the
    shared event-loop thread never read each other's credentials.

    The model is passed to `LitellmModel` *verbatim* (e.g. ``openai/gpt-4o``)
    because `LitellmModel` feeds it straight to ``litellm.acompletion``. The
    ``litellm/`` prefix is an Agent-layer convention to select the backend and
    must NOT be added here -- doing so yields ``litellm/openai/...`` which
    litellm rejects as an unknown provider.
    """
    if bundle is None:
        return None
    from agents import RunConfig

    from openkb.agent.completion_model import CompletionAwareModel

    litellm_model = CompletionAwareModel(
        model=model,
        base_url=bundle.base_url,
        api_key=bundle.api_key,
    )
    return RunConfig(model=litellm_model)
