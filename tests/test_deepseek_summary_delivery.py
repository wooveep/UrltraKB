"""Official DeepSeek summary schemas use the Responses bridge without another budget."""

import pytest


@pytest.mark.parametrize(
    "model,base_url,strict",
    [
        ("deepseek/deepseek-flash", None, True),
        ("deepseek/deepseek-flash", "https://api.deepseek.com", True),
        ("deepseek/deepseek-flash", "https://api.deepseek.com/v1", True),
        ("deepseek/deepseek-flash", "https://custom.invalid/v1", False),
        ("deepseek/deepseek-flash", "https://api.deepseek.com/unknown", False),
        ("deepseek/deepseek-chat", None, False),
    ],
)
def test_deepseek_schema_capability_is_specific_to_the_documented_model_and_endpoint(
    model, base_url, strict
):
    from ultrakb.agent.summary_protocol import summary_response_format

    result = summary_response_format("summary-json", ("section-1",), model=model, base_url=base_url)
    assert result["type"] == ("json_schema" if strict else "json_object")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "generation",
    [
        {},
        {"max_tokens": 17, "reasoning_effort": "low"},
        {"max_tokens": 23, "reasoning_effort": "none"},
    ],
)
async def test_deepseek_strict_summary_reaches_responses_wire_once(
    monkeypatch, asynchronous, generation
):
    import litellm
    from error_transport import ErrorTransport

    from ultrakb.agent.compiler import _llm_call, _llm_call_page_async, _summary_format
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    model = "deepseek/deepseek-flash"
    executor = CompletionExecutor(
        RoleBindings(
            model,
            LlmCredentialBundle(api_key="unused-offline", base_url="https://api.deepseek.com"),
            generation={"compile": generation},
        ),
        ModelCallPolicy(max_calls=1, retries=0),
    )
    method = "acompletion" if asynchronous else "completion"
    real_sdk = getattr(litellm, method)
    selected = []
    with ErrorTransport(status=400) as endpoint:
        # Only redirect the final SDK destination to an error-only loopback
        # service. Capability selection still sees the captured official route.
        def local_options(kwargs):
            selected.append(kwargs)
            return {**kwargs, "base_url": endpoint.url}

        if asynchronous:

            async def local_sdk(**kwargs):
                return await real_sdk(**local_options(kwargs))

        else:

            def local_sdk(**kwargs):
                return real_sdk(**local_options(kwargs))

        monkeypatch.setattr(litellm, method, local_sdk)
        messages = [{"role": "user", "content": "Local summary schema test; return JSON."}]
        with executor.activate(), pytest.raises(litellm.BadRequestError):
            if asynchronous:
                await _llm_call_page_async(
                    model,
                    messages,
                    "document-summary:1",
                    response_protocol="summary-json",
                    expected_sections=("section-1",),
                )
            else:
                _llm_call(
                    model,
                    messages,
                    "summary",
                    response_format=_summary_format("summary-page-json", (), model, None, None),
                )
        assert len(endpoint.requests) == len(selected) == executor.calls == 1
        assert executor.bindings.model("compile") == model
        assert selected[0]["model"] == "openai/responses/deepseek-flash"
        [request] = endpoint.requests
        assert request["path"].endswith("/responses")
        body = request["json"]
        assert body["model"] == "deepseek-flash"
        schema = body["text"]["format"]
        assert schema["type"] == "json_schema" and schema["strict"] is True
        assert schema["schema"]["required"] == (
            ["description", "sections"] if asynchronous else ["description", "content"]
        )
        if "reasoning_effort" in generation:
            assert body["reasoning"]["effort"] == generation["reasoning_effort"]
            assert "reasoning_effort" not in body
        else:
            assert "reasoning" not in body
        limit = generation.get("max_tokens", generation.get("max_completion_tokens"))
        assert body.get("max_output_tokens") == limit


def test_old_deepseek_summary_layout_admission_is_retained():
    from ultrakb.agent.summary_protocol import summary_response_format

    assert summary_response_format(
        "summary-json", ("section-1",), model="deepseek/deepseek-flash", legacy=True
    ) == {"type": "json_object"}


@pytest.mark.asyncio
async def test_legacy_layout_upgrade_checks_schema_capacity_without_sending(kb_dir):
    from types import SimpleNamespace

    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.model_tokenizer import ContextCapacityError, TokenMeasure

    model = "deepseek/deepseek-flash"
    messages = [{"role": "user", "content": "Selected original sections; output JSON."}]
    config = {
        "model": model,
        "model_capacity": {"max_input_tokens": 9000, "tokenizer_model": "gpt-4o-mini"},
    }
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    plain = measure.payload_tokens(
        {"messages": messages, "response_format": {"type": "json_object"}}
    )
    config["model_capacity"]["max_input_tokens"] = plain + 1
    executor = CompletionExecutor(
        RoleBindings(model, LlmCredentialBundle(api_key="unused-offline")),
        ModelCallPolicy(max_calls=1, retries=0),
    )
    executor.compile_capacity = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    checkpoint = CompileCheckpoints(
        kb_dir, SimpleNamespace(task_id="c" * 32, parent_task_id=None), {"doc": "manual"}, {}
    )
    checkpoint.save("summary:1", "original", {"description": "Accepted", "sections": []})
    before = checkpoint.path.read_bytes()
    with executor.activate(), pytest.raises(ContextCapacityError):
        await _llm_call_page_async(
            model,
            messages,
            "document-summary:3",
            response_protocol="summary-json",
            expected_sections=("section-1",),
            checkpoints=checkpoint,
            checkpoint_key="summary:3",
        )
    assert executor.calls == 0
    assert checkpoint.path.read_bytes() == before


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled"])
def test_native_responses_status_survives_the_sdk_chat_bridge(status):
    from types import SimpleNamespace

    import httpx

    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, RoleBindings
    from ultrakb.llm_usage_transport import ModelCall

    # Negative receipt input: no successful model output or network response is
    # fabricated. The SDK bridge can label partial output "stop"; native status
    # must still prevent it from becoming an accepted complete summary.
    observed = ModelCall(None, "deepseek/deepseek-flash", "summary", "a" * 32)
    observed.received(
        "a" * 32,
        httpx.Response(200, json={"object": "response", "status": status, "output": []}),
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content='{"description":')
            )
        ],
        usage=None,
    )
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle(api_key="unused-offline"))
    )
    if status == "incomplete":
        result = executor._result(response, observed, "b" * 32)
        assert result.finish_reason == "length"
    else:
        with pytest.raises(ValueError, match="did not complete"):
            executor._result(response, observed, "b" * 32)
