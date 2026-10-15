"""Small offline admission contracts; no generated response or credential is needed."""

import hashlib

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("tokenizer", ["gpt", "utf8"])
async def test_summary_admission_reserves_every_delivery_repair(kb_dir, monkeypatch, tokenizer):
    from ultrakb import config as config_module
    from ultrakb.agent.document_summary import (
        SYSTEM_TEMPLATE,
        build_document_summary,
        summary_capacity,
        summary_messages,
        window_sections,
    )
    from ultrakb.agent.page_response import page_repair_message
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_capacity import capacity_policy, require_compile_capacity
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        RoleBindings,
    )
    from ultrakb.model_tokenizer import TokenMeasure

    original = (
        "Before maintenance, close the inlet and verify that pressure is zero.\n\n"
        "If inspection detects a leak, keep the inlet closed and replace the damaged seal.\n\n"
        "Only reopen after inspection confirms the seal is intact "
        "and the operator records approval.\n\n"
        "The inspection record belongs in `/srv/inspection/记录`.\n"
    ) * 4
    source = kb_dir / "wiki/sources/manual.md"
    source.write_text(original)
    before = source.read_bytes()
    if tokenizer == "utf8":
        monkeypatch.setattr(config_module, "GLOBAL_CONFIG_DIR", kb_dir / "profile")
        capacity = {**template_capacity(CONNECTION_TEMPLATES[2]), "max_input_tokens": 8000}
    else:
        capacity = {"max_input_tokens": 1800, "tokenizer_model": "gpt-4o-mini"}
    config = {"model": "deepseek/deepseek-flash", "model_capacity": capacity}
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=True))
    admitted = []

    class ObservedWindows(CompileSourceWindows):
        def iter_document_windows(self, **kwargs):
            for window in super().iter_document_windows(**kwargs):
                admitted.append(window)
                yield window

    reader = ObservedWindows(kb_dir / "wiki", "manual")
    system = {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}
    generation = {"max_tokens": 128, "reasoning_effort": "none"}
    parent = next(CompileSourceWindows.iter_document_windows(reader, fits=lambda _: True))
    parent_messages = summary_messages(system, "manual", parent)
    short_repair = page_repair_message(
        "page_truncated_response",
        "LLM [document-summary:1] hit the length limit; skipping to avoid a truncated page",
        response_protocol="summary-json",
        expected_sections=tuple(section["id"] for section in window_sections(parent)),
    )
    # Admit a whole request at its short-repair boundary. Reserving only this
    # shape would let a longer literal correction overflow after a paid response.
    capacity["max_input_tokens"] = measure.payload_tokens(
        {
            "messages": [*parent_messages, short_repair],
            "response_format": {"type": "json_object"},
            **generation,
        }
    )
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=True))
    fits = summary_capacity(config, custom_endpoint=True, generation_options=generation)

    async def forbidden_generation(*args):
        pytest.fail("All windows must be admitted before consuming any generation allowance")

    executor = CompletionExecutor(
        RoleBindings(config["model"], LlmCredentialBundle()),
        ModelCallPolicy(max_calls=0, retries=0),
    )
    with executor.activate(), pytest.raises(ModelBudgetExceeded):
        await build_document_summary(
            reader, system_message=system, generate=forbidden_generation, fits=fits
        )
    assert len(admitted) > 1
    cursor = 0
    for window in admitted:
        assert window["selection_offset"] == cursor
        assert window["sha256"] == hashlib.sha256(before).hexdigest()
        for start, end in window["source_spans"]:
            assert start == cursor and start < end
            cursor = end
        messages = summary_messages(system, "manual", window)
        sections = tuple(section["id"] for section in window_sections(window))
        # Different real validator failures must fit, including dynamic diagnostics
        # that formerly escaped the single, shorter length-repair reservation.
        for code, detail in (
            ("page_truncated_response", "Incomplete summary receipt"),
            ("summary_invalid_json", "Extra JSON follows the object"),
            ("summary_invalid_sections", "Unknown, duplicate or empty section"),
            ("page_excerpt_invalid", "Unknown original excerpt ID"),
            (
                "page_literal_conflict",
                "Code or table syntax must match a complete original excerpt",
            ),
            ("page_numeric_conflict", str([{"unsupported": 97}] * 40)),
            ("summary_invalid_shape", "未通过响应合同检查。" * 100),
        ):
            correction = page_repair_message(
                code, detail, response_protocol="summary-json", expected_sections=sections
            )
            payload = {
                "messages": [*messages, correction],
                "response_format": {"type": "json_object"},
            }
            assert fits(payload["messages"]), (tokenizer, code)
            require_compile_capacity(
                measure,
                {
                    **payload,
                    "model": config["model"],
                    "max_tokens": 128,
                    "extra_body": {"reasoning_effort": "none"},
                    "drop_params": False,
                    "num_retries": 0,
                    "max_retries": 0,
                },
            )
            assert payload["messages"][:-1] == messages
    assert cursor == len(original)
    assert "".join(window["content"] for window in admitted) == original
    assert source.read_bytes() == before
    assert executor.calls == 0
