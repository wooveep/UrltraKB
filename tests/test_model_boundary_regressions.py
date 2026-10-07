"""Real SDK regressions for task settings and provider usage boundaries."""

import pytest
from agents import RunConfig, Runner
from test_managed_agent import agent_for
from test_vendor_sdk import response

from openkb.config import LlmCredentialBundle
from openkb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
from openkb.llm_usage import read_requests
from openkb.llm_usage_execution import import_usage_execution

pytest_plugins = ("test_vendor_sdk",)


def anthropic_stream(initial_usage, final_usage):
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg-fixture",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-sonnet-4-20250514",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": initial_usage,
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": final_usage,
        },
        {"type": "message_stop"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_usage", "input_total", "cached_input"),
    [
        ({"input_tokens": 20, "output_tokens": 1}, 20, 0),
        (
            {
                "input_tokens": 20,
                "output_tokens": 1,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 9,
            },
            36,
            9,
        ),
    ],
)
async def test_anthropic_stream_retains_input_receipt_in_answer_and_ledger(
    model_service, kb_dir, initial_usage, input_total, cached_input
):
    model_service.replies.append((200, anthropic_stream(initial_usage, {"output_tokens": 5})))
    with import_usage_execution(kb_dir, "answer"):
        executor = CompletionExecutor(
            RoleBindings(
                "anthropic/claude-sonnet-4-20250514",
                LlmCredentialBundle(api_key="fixture", base_url=model_service.url),
            ),
            ModelCallPolicy(retries=0),
        )
        result = Runner.run_streamed(
            agent_for(executor), "hello", run_config=RunConfig(tracing_disabled=True)
        )
        receipts = []
        async for event in result.stream_events():
            if event.type == "raw_response_event" and event.data.type == "response.completed":
                receipts.append(event.data.response.openkb_usage)
        assert result.final_output == "ok"
    assert receipts == [
        {
            "input_tokens": input_total,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": cached_input},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    ]
    [record] = read_requests(kb_dir)
    assert (record.input_total, record.output_total, record.cached_input) == (
        input_total,
        5,
        cached_input,
    )
    assert len(model_service.requests) == 1


@pytest.mark.parametrize(("index_limit", "task_limit"), [(1, 5), (5, 1)])
def test_indexing_respects_both_document_and_task_concurrency(
    model_service, tmp_path, index_limit, task_limit
):
    from openkb.index_client import create_index_client
    from openkb.indexer import _build_index_config

    source = tmp_path / "book.md"
    source.write_text("\n".join(f"# Section {i}\nContent for section {i}." for i in range(4)))
    model_service.delay = 0.1
    model_service.replies.extend([(200, response())] * 5)
    bundle = LlmCredentialBundle(api_key="fixture", base_url=model_service.url)
    executor = CompletionExecutor(
        RoleBindings("openai/gpt-4o-mini", bundle), ModelCallPolicy(concurrency=task_limit)
    )
    with executor.activate():
        options = _build_index_config(
            {"model": "openai/gpt-4o-mini", "concurrency": index_limit}, bundle=bundle
        )
        with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
            collection = client.collection()
            document_id = collection.add(str(source))
            assert len(collection.get_document(document_id)["structure"]) == 4

    assert len(model_service.requests) == 5  # Four summaries and one description.
    peak = max(
        sum(begin <= start < end for begin, end in model_service.intervals)
        for start, _ in model_service.intervals
    )
    assert peak == 1


@pytest.mark.asyncio
async def test_pdf_structure_and_async_verification_share_the_index_limit(model_service):
    import asyncio
    import logging
    from types import SimpleNamespace

    from pageindex.config import max_concurrency_scope
    from pageindex.index.page_index import meta_processor
    from pageindex.index.utils import llm_acompletion
    from pageindex.llm import llm_client_scope

    from openkb.index_llm import PageIndexLLM

    model_service.delay = 0.15
    model_service.replies.extend(
        [
            (200, response()),
            (
                200,
                response(
                    {"role": "assistant", "content": '[{"title":"Section","physical_index":1}]'}
                ),
            ),
            (200, response({"role": "assistant", "content": '{"answer":"yes"}'})),
        ]
    )
    executor = CompletionExecutor(
        RoleBindings(
            "openai/gpt-4o-mini",
            LlmCredentialBundle(api_key="fixture", base_url=model_service.url),
        ),
        ModelCallPolicy(concurrency=5, retries=0),
    )
    with max_concurrency_scope(1), llm_client_scope(PageIndexLLM(executor)):
        verification = asyncio.create_task(llm_acompletion("openai/gpt-4o-mini", "verify sibling"))
        while not model_service.requests:
            await asyncio.sleep(0.01)
        structure = await meta_processor(
            [("Section", 1)],
            opt=SimpleNamespace(model="openai/gpt-4o-mini"),
            logger=logging.getLogger(__name__),
        )
        assert structure == [{"title": "Section", "physical_index": 1}]
        assert await verification == "ok"
    assert len(model_service.requests) == 3
    assert (
        max(
            sum(begin <= start < end for begin, end in model_service.intervals)
            for start, _ in model_service.intervals
        )
        == 1
    )


@pytest.mark.asyncio
async def test_cancelled_index_stage_settles_its_sync_request(model_service):
    import asyncio

    from pageindex.config import max_concurrency_scope
    from pageindex.index.utils import llm_completion
    from pageindex.llm import llm_client_scope, run_sync

    from openkb.index_llm import PageIndexLLM

    model_service.delay = 0.15
    model_service.replies.extend([(200, response())] * 2)
    executor = CompletionExecutor(
        RoleBindings(
            "openai/gpt-4o-mini",
            LlmCredentialBundle(api_key="fixture", base_url=model_service.url),
        ),
        ModelCallPolicy(concurrency=1, retries=0),
    )
    with max_concurrency_scope(1), llm_client_scope(PageIndexLLM(executor)):
        pending = asyncio.create_task(run_sync(llm_completion, "openai/gpt-4o-mini", "summary"))
        while not model_service.requests:
            await asyncio.sleep(0.01)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done()
        pending.cancel()  # Repeated cancellation still cannot abandon the worker.
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert model_service.intervals[0][1] != float("inf")
        assert await run_sync(llm_completion, "openai/gpt-4o-mini", "next summary") == "ok"
    assert len(model_service.requests) == 2


def test_index_waiters_do_not_starve_the_sdk_thread_pool(model_service):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from pageindex.config import max_concurrency_scope
    from pageindex.index.utils import llm_acompletion, llm_completion
    from pageindex.llm import llm_client_scope, run_sync

    from openkb.index_llm import PageIndexLLM

    model_service.replies.extend([(200, response())] * 2)
    executor = CompletionExecutor(
        RoleBindings(
            "openai/gpt-4o-mini",
            LlmCredentialBundle(api_key="fixture", base_url=model_service.url),
        ),
        ModelCallPolicy(concurrency=5, retries=0),
    )

    async def run():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        with max_concurrency_scope(1), llm_client_scope(PageIndexLLM(executor)):
            return await asyncio.wait_for(
                asyncio.gather(
                    run_sync(llm_completion, "openai/gpt-4o-mini", "structure"),
                    llm_acompletion("openai/gpt-4o-mini", "verification"),
                ),
                timeout=3,
            )

    assert asyncio.run(run()) == ["ok", "ok"]
    assert len(model_service.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["sync", "async", "agent", "stream"])
@pytest.mark.parametrize("later_source", ["environment", "global"])
async def test_missing_task_credential_cannot_use_a_later_key(
    model_service, kb_dir, tmp_path, monkeypatch, channel, later_source
):
    import litellm

    from openkb import config
    from openkb.application.execution import ExecutionContext
    from openkb.llm_execution import ModelRequest
    from openkb.locks import kb_ingest_lock

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global/global.yaml")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(litellm, "api_key", None)
    monkeypatch.setattr(litellm, "openai_key", None)
    (kb_dir / ".openkb/config.yaml").write_text("model: openai/gpt-4o-mini\nlanguage: en\n")
    request = ModelRequest(
        [{"role": "user", "content": "hello"}], operation="compile", stage="summary"
    )
    from test_vendor_sdk import streamed_response

    reply = (
        streamed_response({"role": "assistant", "content": "ok"})
        if channel == "stream"
        else response()
    )
    model_service.replies.append((200, reply))
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        if later_source == "environment":
            monkeypatch.setenv("OPENAI_API_KEY", "late-fixture-key")
        else:
            monkeypatch.setattr(litellm, "api_key", "late-fixture-key")
        with pytest.raises(litellm.AuthenticationError, match="captured"):
            if channel == "sync":
                context.executor.complete("compile", request)
            elif channel == "async":
                await context.executor.acomplete("compile", request)
            elif channel == "agent":
                await Runner.run(
                    agent_for(context.executor),
                    "hello",
                    run_config=RunConfig(tracing_disabled=True),
                )
            else:
                result = Runner.run_streamed(
                    agent_for(context.executor),
                    "hello",
                    run_config=RunConfig(tracing_disabled=True),
                )
                async for _ in result.stream_events():
                    pass
    assert model_service.requests == []

    if later_source == "environment":
        model_service.replies[:] = [(200, response())]
        with kb_ingest_lock(kb_dir / ".openkb"), ExecutionContext().begin(kb_dir):
            from openkb.llm_execution import active_executor

            assert (await active_executor().acomplete("compile", request)).text == "ok"
        headers = {key.lower(): value for key, value in model_service.records[0][1].items()}
        assert headers["authorization"] == "Bearer late-fixture-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chatgpt", "github_copilot", "ollama_chat"])
async def test_sdk_auth_still_reaches_transport_without_a_captured_api_key(
    model_service, tmp_path, monkeypatch, provider
):
    from openkb.llm_execution import ModelRequest

    if provider == "ollama_chat":
        reply = {
            "model": "llama3",
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
            "prompt_eval_count": 12,
            "eval_count": 3,
        }
        model = "ollama_chat/llama3"
    else:
        # Replace only the external subscription authenticator; keep the real
        # provider transformation, executor, and HTTP client in the test.
        from importlib import import_module

        monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "chatgpt"))
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_DIR", str(tmp_path / "copilot"))
        authenticator = import_module(f"litellm.llms.{provider}.authenticator").Authenticator
        token_method = "get_access_token" if provider == "chatgpt" else "get_api_key"
        monkeypatch.setattr(authenticator, token_method, lambda self: "subscription-fixture")
        monkeypatch.setattr(authenticator, "get_api_base", lambda self: model_service.url)
        if provider == "chatgpt":
            monkeypatch.setattr(authenticator, "get_account_id", lambda self: "fixture-account")
        model = f"{provider}/gpt-4o"
        reply = response()

    model_service.replies.append((200, reply))
    executor = CompletionExecutor(
        RoleBindings(model, LlmCredentialBundle(base_url=model_service.url)),
        ModelCallPolicy(retries=0),
    )
    request = ModelRequest(
        [{"role": "user", "content": "hello"}], operation="compile", stage="summary"
    )
    assert (await executor.acomplete("compile", request)).text == "ok"
    assert len(model_service.requests) == 1
    headers = {key.lower(): value for key, value in model_service.records[0][1].items()}
    if provider != "ollama_chat":
        assert headers["authorization"] == "Bearer subscription-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_api_key", [False, True])
async def test_azure_workload_auth_without_a_key_rejects_later_api_key_fallback(
    model_service, monkeypatch, late_api_key
):
    import litellm

    from openkb.llm_execution import ModelRequest

    for name in ("AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(litellm, "api_key", None)
    monkeypatch.setattr(litellm, "azure_key", None)
    if late_api_key:
        monkeypatch.delenv("AZURE_OPENAI_AD_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "workload-fixture-token")
    executor = CompletionExecutor(
        RoleBindings("azure/gpt-4o-mini", LlmCredentialBundle(base_url=model_service.url)),
        ModelCallPolicy(retries=0),
    )
    request = ModelRequest(
        [{"role": "user", "content": "hello"}], operation="compile", stage="summary"
    )
    model_service.replies.append((200, response()))
    if late_api_key:
        monkeypatch.setattr(litellm, "azure_key", "late-fixture-key")
        with pytest.raises(litellm.AuthenticationError, match="captured"):
            await executor.acomplete("compile", request)
        assert model_service.requests == []
    else:
        assert (await executor.acomplete("compile", request)).text == "ok"
        [(_, headers, _)] = model_service.records
        assert {key.lower(): value for key, value in headers.items()}["authorization"] == (
            "Bearer workload-fixture-token"
        )


@pytest.mark.asyncio
async def test_keyless_local_provider_cannot_send_a_later_global_key(model_service, monkeypatch):
    import litellm

    from openkb.llm_execution import ModelRequest

    executor = CompletionExecutor(
        RoleBindings("ollama_chat/llama3", LlmCredentialBundle(base_url=model_service.url)),
        ModelCallPolicy(retries=0),
    )
    monkeypatch.setattr(litellm, "api_key", "late-fixture-key")
    model_service.replies.append(
        (200, {"model": "llama3", "message": {"role": "assistant", "content": "ok"}, "done": True})
    )
    request = ModelRequest(
        [{"role": "user", "content": "hello"}], operation="compile", stage="summary"
    )
    try:
        result = await executor.acomplete("compile", request)
    except litellm.AuthenticationError:
        assert model_service.requests == []
    else:
        assert result.text == "ok"
        [(_, headers, _)] = model_service.records
        assert "authorization" not in {key.lower() for key in headers}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["cohere", "cohere_chat"])
async def test_native_message_payload_cannot_bypass_frozen_credentials(
    model_service, monkeypatch, provider
):
    import litellm

    from openkb.llm_execution import ModelRequest

    executor = CompletionExecutor(
        RoleBindings(f"{provider}/v1/command-r", LlmCredentialBundle(base_url=model_service.url)),
        ModelCallPolicy(retries=0),
    )
    monkeypatch.setattr(litellm, "api_key", "late-fixture-key")
    monkeypatch.setenv("COHERE_API_KEY", "late-fixture-key")
    model_service.replies.append((200, {"text": "ok", "generation_id": "fixture"}))
    request = ModelRequest(
        [{"role": "user", "content": "hello"}], operation="compile", stage="summary"
    )
    with pytest.raises(litellm.AuthenticationError, match="captured"):
        await executor.acomplete("compile", request)
    assert model_service.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure_ai", "databricks"])
@pytest.mark.parametrize("late_api_key", [False, True])
async def test_workload_bearer_auth_is_distinct_from_late_api_key_fallback(
    model_service, monkeypatch, provider, late_api_key
):
    import litellm
    from litellm.llms.databricks.common_utils import DatabricksBase

    from openkb.llm_execution import ModelRequest

    for name in ("AZURE_AI_API_KEY", "DATABRICKS_API_KEY", "AZURE_AD_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(litellm, "api_key", None)
    if not late_api_key:
        if provider == "azure_ai":
            monkeypatch.setenv("AZURE_AD_TOKEN", "workload-fixture-token")
        else:
            monkeypatch.setenv("DATABRICKS_CLIENT_ID", "fixture-client")
            monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "fixture-secret")
            monkeypatch.setattr(
                DatabricksBase,
                "_get_oauth_m2m_token",
                lambda *args, **kwargs: "workload-fixture-token",
            )
    model = f"{provider}/Meta-Llama-3.1-8B-Instruct"
    executor = CompletionExecutor(
        RoleBindings(model, LlmCredentialBundle(base_url=model_service.url)),
        ModelCallPolicy(retries=0),
    )
    request = ModelRequest(
        [{"role": "user", "content": "hello"}], operation="compile", stage="summary"
    )
    model_service.replies.append((200, response()))
    if late_api_key:
        monkeypatch.setenv(provider.upper() + "_API_KEY", "late-fixture-key")
        with pytest.raises(litellm.AuthenticationError, match="captured"):
            await executor.acomplete("compile", request)
        assert model_service.requests == []
    else:
        assert (await executor.acomplete("compile", request)).text == "ok"
        [(_, headers, _)] = model_service.records
        assert {key.lower(): value for key, value in headers.items()}["authorization"] == (
            "Bearer workload-fixture-token"
        )
