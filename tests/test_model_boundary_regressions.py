"""Real SDK regressions for task settings and provider usage boundaries."""

import pytest
from agents import RunConfig, Runner
from test_managed_agent import agent_for

from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize(
    ("initial_usage", "input_total", "cached_input"),
    [
        ({"input_tokens": 20, "output_tokens": 1}, 20, None),
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
def test_anthropic_stream_retains_input_receipt_in_answer_and_ledger(
    initial_usage, input_total, cached_input
):
    from ultrakb.llm_usage import normalize_usage
    from ultrakb.stream_receipts import merge_receipts

    # Direct parser inputs describe protocol fields, not fabricated SDK/network replies.
    receipt = merge_receipts(
        [
            {"message": {"id": "message", "usage": initial_usage}},
            {"usage": {"output_tokens": 5, "input_tokens": None}},
        ]
    )
    assert receipt["id"] == "message"
    parsed = normalize_usage(receipt["usage"])
    assert (parsed["input_total"], parsed["output_total"], parsed["cached_input"]) == (
        input_total,
        5,
        cached_input,
    )
    assert parsed["usage_status"] == "known"


@pytest.mark.parametrize(("index_limit", "task_limit"), [(1, 5), (5, 1)])
def test_indexing_respects_both_document_and_task_concurrency(
    model_service, tmp_path, index_limit, task_limit
):
    from ultrakb.index_client import create_index_client
    from ultrakb.indexer import _build_index_config

    source = tmp_path / "book.md"
    source.write_text("\n".join(f"# Section {i}\nContent for section {i}." for i in range(4)))
    model_service.delay = 0.1
    bundle = model_service.credentials
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", bundle), ModelCallPolicy(concurrency=task_limit)
    )
    with executor.activate():
        options = _build_index_config(
            {"model": "deepseek/deepseek-flash", "concurrency": index_limit}, bundle=bundle
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

    from ultrakb.index_llm import PageIndexLLM

    model_service.delay = 0.15
    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            model_service.credentials,
        ),
        ModelCallPolicy(concurrency=5, retries=0),
    )
    with max_concurrency_scope(1), llm_client_scope(PageIndexLLM(executor)):
        verification = asyncio.create_task(
            llm_acompletion("deepseek/deepseek-flash", "Reply exactly ok in lowercase.")
        )
        while not model_service.requests:
            await asyncio.sleep(0.01)
        structure = await meta_processor(
            [("Section", 1)],
            opt=SimpleNamespace(model="deepseek/deepseek-flash"),
            logger=logging.getLogger(__name__),
        )
        assert len(structure) == 1
        assert structure[0]["physical_index"] == 1 and structure[0]["title_origin"] == "generated"
        assert structure[0]["title"]
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

    from ultrakb.index_llm import PageIndexLLM

    model_service.delay = 0.15
    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            model_service.credentials,
        ),
        ModelCallPolicy(concurrency=1, retries=0),
    )
    with max_concurrency_scope(1), llm_client_scope(PageIndexLLM(executor)):
        pending = asyncio.create_task(
            run_sync(llm_completion, "deepseek/deepseek-flash", "Reply exactly ok in lowercase.")
        )
        while not model_service.requests:
            await asyncio.sleep(0.01)
        pending.cancel()
        await asyncio.sleep(0.01)
        assert not pending.done()
        pending.cancel()  # Repeated cancellation still cannot abandon the worker.
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert model_service.intervals[0][1] != float("inf")
        assert (
            await run_sync(
                llm_completion, "deepseek/deepseek-flash", "Reply exactly ok in lowercase."
            )
            == "ok"
        )
    assert len(model_service.requests) == 2


def test_index_waiters_do_not_starve_the_sdk_thread_pool(model_service):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from pageindex.config import max_concurrency_scope
    from pageindex.index.utils import llm_acompletion, llm_completion
    from pageindex.llm import llm_client_scope, run_sync

    from ultrakb.index_llm import PageIndexLLM

    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            model_service.credentials,
        ),
        ModelCallPolicy(concurrency=5, retries=0),
    )

    async def run():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        with max_concurrency_scope(1), llm_client_scope(PageIndexLLM(executor)):
            return await asyncio.wait_for(
                asyncio.gather(
                    run_sync(
                        llm_completion, "deepseek/deepseek-flash", "Reply exactly ok in lowercase."
                    ),
                    llm_acompletion("deepseek/deepseek-flash", "Reply exactly ok in lowercase."),
                ),
                timeout=30,
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

    from ultrakb import config
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_execution import ModelRequest
    from ultrakb.locks import kb_ingest_lock

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global/global.yaml")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(litellm, "api_key", None)
    monkeypatch.setattr(litellm, "openai_key", None)
    (kb_dir / ".ultrakb/config.yaml").write_text("model: openai/gpt-4o-mini\nlanguage: en\n")
    request = ModelRequest(
        [{"role": "user", "content": "Reply exactly ok in lowercase."}],
        operation="compile",
        stage="summary",
        generation_options={"max_tokens": 64},
    )
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        if later_source == "environment":
            monkeypatch.setenv("OPENAI_API_KEY", model_service.credentials.api_key)
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
        with kb_ingest_lock(kb_dir / ".ultrakb"), ExecutionContext().begin(kb_dir):
            from ultrakb.llm_execution import active_executor

            assert (await active_executor().acomplete("compile", request)).text == "ok"
        assert "authorization" in {key.lower() for key in model_service.records[0][1]}


@pytest.mark.parametrize("provider", ["chatgpt", "github_copilot", "ollama_chat"])
def test_sdk_auth_still_reaches_transport_without_a_captured_api_key(model_service, provider):
    bindings = RoleBindings(f"{provider}/configured-model", LlmCredentialBundle())
    headers = {} if provider == "ollama_chat" else {"authorization": "Bearer subscription-token"}
    bindings.validate_request_credentials("compile", headers, selected_auth="sdk")
    assert bindings.credentials().api_key is None
    assert model_service.requests == []


@pytest.mark.parametrize("late_api_key", [False, True])
def test_azure_workload_auth_without_a_key_rejects_later_api_key_fallback(
    model_service, late_api_key
):
    import litellm

    bindings = RoleBindings("azure/configured-model", LlmCredentialBundle())
    if late_api_key:
        with pytest.raises(litellm.AuthenticationError, match="captured"):
            bindings.validate_request_credentials(
                "compile", {"api-key": "later-key"}, selected_auth="api_key"
            )
    else:
        bindings.validate_request_credentials(
            "compile", {"authorization": "Bearer workload-token"}, selected_auth="sdk"
        )
    assert model_service.requests == []


def test_keyless_local_provider_cannot_send_a_later_global_key(model_service):
    import litellm

    bindings = RoleBindings("ollama_chat/configured-model", LlmCredentialBundle())
    bindings.validate_request_credentials("compile", {})
    with pytest.raises(litellm.AuthenticationError, match="captured"):
        bindings.validate_request_credentials(
            "compile", {"authorization": "Bearer later-key"}, selected_auth="api_key"
        )
    captured = RoleBindings(
        "ollama_chat/configured-model",
        LlmCredentialBundle(extra_headers={"authorization": "Bearer explicit"}),
    )
    captured.validate_request_credentials("compile", {"authorization": "Bearer explicit"})
    assert model_service.requests == []


@pytest.mark.parametrize("provider", ["cohere", "cohere_chat"])
def test_native_message_payload_cannot_bypass_frozen_credentials(model_service, provider):
    import litellm

    from ultrakb.llm_execution import ModelRequest

    bindings = RoleBindings(f"{provider}/configured-model", LlmCredentialBundle())
    request = ModelRequest(
        [{"role": "user", "content": "Question"}], operation="compile", stage="summary"
    )
    assert request.payload()["messages"] == [{"role": "user", "content": "Question"}]
    with pytest.raises(litellm.AuthenticationError, match="captured"):
        bindings.validate_request_credentials(
            "compile", {"authorization": "Bearer later-key"}, selected_auth="api_key"
        )
    assert model_service.requests == []


@pytest.mark.parametrize("provider", ["azure_ai", "databricks"])
@pytest.mark.parametrize("late_api_key", [False, True])
def test_workload_bearer_auth_is_distinct_from_late_api_key_fallback(
    model_service, provider, late_api_key
):
    import litellm

    bindings = RoleBindings(f"{provider}/configured-model", LlmCredentialBundle())
    headers = (
        {"authorization": "Bearer later-key"}
        if late_api_key
        else {"authorization": "Bearer workload-token"}
    )
    if late_api_key:
        with pytest.raises(litellm.AuthenticationError, match="captured"):
            bindings.validate_request_credentials("compile", headers, selected_auth="api_key")
    else:
        bindings.validate_request_credentials("compile", headers, selected_auth="sdk")
    assert model_service.requests == []
