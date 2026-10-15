"""Observe precedence after real SDK serialization, using HTTP errors only."""

import litellm
import pytest
from error_transport import ErrorTransport

from ultrakb.config import LlmCredentialBundle
from ultrakb.index_llm import PageIndexLLM
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, ModelRequest, RoleBindings


def index_executor(service):
    return CompletionExecutor(
        RoleBindings(
            "openai/gpt-5",
            LlmCredentialBundle(api_key="offline-parameter-key", base_url=service.url),
            model_table={},
            generation={"document_index": {"max_tokens": 64}},
        ),
        ModelCallPolicy(max_calls=1, retries=0),
    )


@pytest.mark.integration
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.asyncio
async def test_index_request_compatibility_survives_role_defaults_at_the_real_sdk_exit(
    asynchronous,
):
    # This is an error-only local endpoint, never an OpenAI model completion.
    # The GPT SDK transformation rejects temperature=0 unless the caller's
    # explicit drop_params=True survives the application's parameter merge.
    with ErrorTransport(status=400) as service:
        executor = index_executor(service)
        adapter = PageIndexLLM(executor)
        messages = [{"role": "user", "content": "Parameter precedence; reject this request."}]
        with pytest.raises(litellm.BadRequestError):
            if asynchronous:
                await adapter.acomplete(messages, stage="structure")
            else:
                adapter.complete(messages, stage="structure")
        assert len(service.requests) == executor.calls == 1
        wire = service.requests[0]
        assert wire["path"] == "/v1/chat/completions"
        assert wire["json"]["model"] == "gpt-5"
        assert wire["json"]["messages"] == messages
        assert wire["json"]["max_completion_tokens"] == 64
        assert "temperature" not in wire["json"]


@pytest.mark.integration
def test_explicit_strict_request_still_rejects_unsupported_parameters_before_http():
    with ErrorTransport(status=400) as service:
        executor = index_executor(service)
        request = ModelRequest(
            [{"role": "user", "content": "Strict parameter contract."}],
            operation="document_index",
            stage="structure",
            generation_options={"temperature": 0, "drop_params": False},
        )
        with pytest.raises(litellm.UnsupportedParamsError):
            executor.complete("document_index", request)
        assert service.requests == [] and executor.calls == 0
