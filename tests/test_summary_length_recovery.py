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


@pytest.mark.parametrize("failure", ["child_length", "parent_protocol", "allowance"])
def test_summary_recovery_stops_without_another_split_or_fresh_allowance(
    kb_dir, compile_work, failure
):
    from ultrakb.agent.document_summary import summary_messages
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import digest
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        RoleBindings,
    )

    _, summary, reader, system, parent, messages = retained_summary(kb_dir, "text")
    before = summary.read_bytes()
    checkpoints = compile_work({"document": "manual"}, {"protocol": "summary-json"})
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
    restored = compile_work({"document": "manual"}, {"protocol": "summary-json"})
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        restored.require_generation(key, signature)
    if failure == "allowance":
        bindings = RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle())
        policy = ModelCallPolicy(max_calls=4, retries=0)
        executor = CompletionExecutor(bindings, policy)
        executor.budget.restore(4, {"compile": 4})
        resumed = executor
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
