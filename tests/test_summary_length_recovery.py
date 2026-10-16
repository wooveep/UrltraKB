"""Summary splitting and durable recovery use small source inputs and real generation."""

import json

import pytest


def retained_summary(kb_dir, source_kind):
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, summary_messages
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.locks import atomic_write_text

    original = "Keep the inlet closed.\n\nOnly reopen after inspection."
    suffix = "md" if source_kind == "text" else "json"
    atomic_write_text(
        kb_dir / f"wiki/sources/manual.{suffix}",
        original
        if source_kind == "text"
        else json.dumps(
            [
                {"page": 1, "unit_kind": "page", "content": original},
            ]
        ),
    )
    summary = kb_dir / "wiki/summaries/manual.md"
    atomic_write_text(
        summary, f"---\nfull_text: sources/manual.{suffix}\n---\nPrevious successful result."
    )
    reader = CompileSourceWindows(kb_dir / "wiki", "manual", navigation=[])
    system = {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}
    parent = next(reader.iter_document_windows(fits=lambda _: True))
    messages = summary_messages(system, "manual", parent)
    return original, summary, reader, system, parent, messages


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["text", "pages"])
@pytest.mark.parametrize("stop_at", ["completed_child", "split_preparation", "received_repair"])
async def test_split_summary_resumes_completed_child_with_original_task_allowance(
    kb_dir, source_kind, stop_at, monkeypatch, live_llm
):
    from test_document_summary import generate_summary

    from ultrakb.agent.compiler import _write_summary
    from ultrakb.agent.document_summary import (
        build_document_summary,
        summary_messages,
        window_sections,
    )
    from ultrakb.compile_checkpoints import CompileCheckpoints, digest
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.locks import LockCancelled
    from ultrakb.model_task import TaskJournal, read_task

    original, summary, reader, system, parent, messages = retained_summary(kb_dir, source_kind)
    before = summary.read_bytes()
    live_llm.install(monkeypatch)
    stopped = False
    bindings = RoleBindings("deepseek/deepseek-flash", live_llm.credentials)
    policy = ModelCallPolicy(max_calls=5, retries=0, cancelled=lambda: stopped)
    executor = CompletionExecutor(bindings, policy)
    # The old task allowance is a persisted recovery input, independent of
    # this run's provider invoices. No long or truncated model output is requested.
    historical_used = 3 if stop_at == "completed_child" else 2
    executor.budget.restore(historical_used, {"compile": historical_used})
    journal = TaskJournal(kb_dir, executor)
    checkpoints = CompileCheckpoints(
        kb_dir, journal.record, {"document": "manual"}, {"protocol": "summary-json"}
    )
    checkpoints.claim_repair("summary:1", digest(messages))
    if stop_at == "received_repair":
        stopped = True
    with executor.activate():
        checkpoints.record_failure("summary:1", digest(messages), "page_truncated_response")
    stopped = False
    assert checkpoints.failure_code("summary:1", digest(messages)) == "page_truncated_response"

    async def forbidden(*args):
        raise AssertionError("No generation is allowed while the local split is interrupted")

    if stop_at == "split_preparation":

        def interrupted_split(window):
            raise LockCancelled("Local cancellation before writing the split marker")

        with monkeypatch.context() as patch:
            patch.setattr(reader, "split_document_window", interrupted_split)
            with executor.activate(), pytest.raises(LockCancelled):
                await build_document_summary(
                    reader,
                    system_message=system,
                    generate=forbidden,
                    fits=lambda _: True,
                    checkpoints=checkpoints,
                )
    children = reader.split_document_window(parent)
    signatures = [digest(summary_messages(system, "manual", child)) for child in children]
    assert len(children) == 2
    if stop_at == "completed_child":
        checkpoints.save("summary-split:1", digest(messages), signatures)
        accepted = {
            "description": "First accepted condition",
            "sections": [
                {
                    "id": section["id"],
                    "content": children[0]["content"][section["start"] : section["end"]],
                }
                for section in window_sections(children[0])
            ],
        }
        checkpoints.save("summary:1.1", signatures[0], accepted)

    resumed = CompletionExecutor(bindings, policy)
    TaskJournal(kb_dir, resumed, resume=journal.record.task_id)
    restored = CompileCheckpoints(
        kb_dir, journal.record, {"document": "manual"}, checkpoints.conditions
    )
    sent = []

    async def generate(request, stage, originals, sections):
        sent.append(request)
        return await generate_summary(request, stage, originals, sections, live_llm)

    with resumed.activate():
        draft = await build_document_summary(
            reader,
            system_message=system,
            generate=generate,
            fits=lambda _: True,
            checkpoints=restored,
        )
    assert len(sent) == (1 if stop_at == "completed_child" else 2)
    assert resumed.calls == read_task(kb_dir, journal.record.task_id).used == 4
    assert summary.read_bytes() == before
    expected = original if source_kind == "text" else "[Physical page 1]\n\n" + original
    assert "".join(child["content"] for child in children) == expected
    assert children[0]["selection_offset"] == 0
    assert children[1]["selection_offset"] == len(children[0]["content"])
    if source_kind == "text":
        assert children[0]["source_spans"][-1][1] == children[1]["source_spans"][0][0]
        assert children[-1]["source_spans"][-1][1] == len(original)
    else:
        assert children[0]["page_spans"][-1]["end"] == children[1]["page_spans"][0]["start"]
        assert children[-1]["page_spans"][-1]["end"] == len(original)
    assert all(
        context["primary"] is False
        for child in children
        for context in child["supplementary_context"]
    )
    with resumed.activate():
        cached = await build_document_summary(
            reader,
            system_message=system,
            generate=forbidden,
            fits=lambda _: True,
            checkpoints=restored,
        )
    assert cached == draft and resumed.calls == 4
    _write_summary(
        kb_dir / "wiki",
        "manual",
        draft.markdown,
        doc_type="pageindex",
        description=draft.description,
        full_text=f"sources/manual.{'md' if source_kind == 'text' else 'json'}",
    )
    saved = summary.read_text().lower()
    assert "inspection" in saved and "inlet" in saved and "closed" in saved


@pytest.mark.parametrize("failure", ["child_length", "parent_protocol", "allowance"])
def test_summary_recovery_stops_without_another_split_or_fresh_allowance(kb_dir, failure):
    from types import SimpleNamespace

    from ultrakb.agent.document_summary import summary_messages
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import CompileCheckpoints, digest
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        RoleBindings,
    )
    from ultrakb.model_task import TaskJournal

    _, summary, reader, system, parent, messages = retained_summary(kb_dir, "text")
    before = summary.read_bytes()
    task = SimpleNamespace(task_id="a" * 32, parent_task_id=None)
    checkpoints = CompileCheckpoints(
        kb_dir, task, {"document": "manual"}, {"protocol": "summary-json"}
    )
    key, signature = "summary:1", digest(messages)
    if failure == "parent_protocol":
        with pytest.raises(PageResponseError):
            validate_page_delivery(
                "{}", response_protocol="summary-json", expected_sections=("section-1",)
            )
    else:
        children = reader.split_document_window(parent)
        assert len(children) == 2
        key, signature = "summary:1.1", digest(summary_messages(system, "manual", children[0]))
        checkpoints.save(
            "summary-split:1",
            digest(messages),
            [digest(summary_messages(system, "manual", child)) for child in children],
        )
    checkpoints.claim_repair(key, signature)
    checkpoints.record_failure(
        key,
        signature,
        "summary_invalid_json" if failure == "parent_protocol" else "page_truncated_response",
    )
    restored = CompileCheckpoints(kb_dir, task, {"document": "manual"}, checkpoints.conditions)
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        restored.require_generation(key, signature)
    if failure == "allowance":
        bindings = RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle())
        policy = ModelCallPolicy(max_calls=4, retries=0)
        executor = CompletionExecutor(bindings, policy)
        executor.budget.restore(4, {"compile": 4})
        journal = TaskJournal(kb_dir, executor)
        resumed = CompletionExecutor(bindings, policy)
        TaskJournal(kb_dir, resumed, resume=journal.record.task_id)
        with pytest.raises(ModelBudgetExceeded):
            resumed.budget.require("compile", 1)
        assert resumed.calls == 4
    assert restored.load(key, signature) is None
    assert summary.read_bytes() == before


@pytest.mark.parametrize(
    "model,limit", [("openai/gpt-4o-mini", 1800), ("deepseek/deepseek-chat", 1795)]
)
@pytest.mark.asyncio
async def test_summary_window_admits_its_length_repair_before_using_the_allowance(
    kb_dir, model, limit
):
    import hashlib

    from ultrakb.agent.document_summary import (
        SYSTEM_TEMPLATE,
        build_document_summary,
        summary_capacity,
        summary_messages,
        window_sections,
    )
    from ultrakb.agent.page_response import PageResponseError, page_repair_message
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.agent.summary_protocol import summary_response_format
    from ultrakb.compile_checkpoints import digest
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.locks import atomic_write_text
    from ultrakb.model_tokenizer import TokenMeasure

    original = (
        "Before maintenance, close the inlet and verify that pressure is zero.\n\n"
        "If inspection detects a leak, keep the inlet closed and replace the damaged seal.\n\n"
        "Only reopen after inspection confirms the seal is intact "
        "and the operator records approval."
    )
    source_path = kb_dir / "wiki/sources/manual.md"
    atomic_write_text(source_path, original)
    source_bytes = source_path.read_bytes()
    reader = CompileSourceWindows(kb_dir / "wiki", "manual")
    system = {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}
    config = {
        "model": model,
        "model_capacity": {"max_input_tokens": limit, "tokenizer_model": "gpt-4o-mini"},
    }
    fits = summary_capacity(config, custom_endpoint=False)
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))

    def admitted(window):
        messages = summary_messages(system, "manual", window)
        correction = page_repair_message(
            "page_truncated_response",
            "Incomplete summary receipt",
            response_protocol="summary-json",
            expected_sections=tuple(s["id"] for s in window_sections(window)),
        )
        return fits(messages) and fits([*messages, correction])

    windows = list(reader.iter_document_windows(fits=admitted))
    assert windows
    assert "".join(window["content"] for window in windows) == original
    cursor = 0
    reconstructed = []
    for window in windows:
        assert window["selection_offset"] == cursor
        assert window["sha256"] == hashlib.sha256(source_bytes).hexdigest()
        parts = []
        for start, end in window["source_spans"]:
            assert start == cursor
            assert start < end <= len(original)
            parts.append(original[start:end])
            cursor = end
        assert window["content"] == "".join(parts)
        reconstructed.extend(parts)
        original_messages = summary_messages(system, "manual", window)
        signature = digest(original_messages)
        correction = page_repair_message(
            "page_truncated_response",
            "Incomplete summary receipt",
            response_protocol="summary-json",
            expected_sections=tuple(s["id"] for s in window_sections(window)),
        )
        repair_messages = [*original_messages, correction]
        assert len(repair_messages) == len(original_messages) + 1
        assert repair_messages[:-1] == original_messages
        assert digest(repair_messages[:-1]) == signature
        assert "failed validation (page_truncated_response)" in str(correction)
        assert (
            measure.payload_tokens(
                {
                    "messages": repair_messages,
                    "response_format": summary_response_format(
                        "summary-json", tuple(s["id"] for s in window_sections(window)), model=model
                    ),
                }
            )
            <= limit
        )
    assert cursor == len(original)
    assert "".join(reconstructed) == original

    # Admission must include the correction envelope even when the original
    # request fits. This boundary needs no model response or spent allowance.
    parent = next(reader.iter_document_windows(fits=lambda _: True))
    messages = summary_messages(system, "manual", parent)
    normal_tokens = measure.payload_tokens(
        {
            "messages": messages,
            "response_format": summary_response_format(
                "summary-json", tuple(s["id"] for s in window_sections(parent)), model=model
            ),
        }
    )
    tight_fits = summary_capacity(
        {
            **config,
            "model_capacity": {
                **config["model_capacity"],
                "max_input_tokens": normal_tokens,
            },
        },
        custom_endpoint=False,
    )
    assert tight_fits(messages)
    correction = page_repair_message(
        "page_truncated_response",
        "Incomplete summary receipt",
        response_protocol="summary-json",
        expected_sections=tuple(s["id"] for s in window_sections(parent)),
    )
    assert not tight_fits([*messages, correction])

    async def forbidden_generation(*args):
        raise AssertionError("Capacity admission must finish before requesting generation")

    executor = CompletionExecutor(
        RoleBindings(model, LlmCredentialBundle()), ModelCallPolicy(max_calls=2, retries=0)
    )
    with (
        executor.activate(),
        pytest.raises(PageResponseError, match="document_summary_capacity_exceeded"),
    ):
        await build_document_summary(
            reader,
            system_message=system,
            generate=forbidden_generation,
            fits=tight_fits,
        )
    assert executor.calls == 0
    assert source_path.read_bytes() == source_bytes
