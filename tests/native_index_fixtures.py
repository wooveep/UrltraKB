"""The project's real PageIndex transport with a small request/output allowance."""

import pytest


@pytest.fixture
def native_index_llm(live_llm, monkeypatch):
    from pageindex.llm import llm_client_scope

    from ultrakb.index_llm import PageIndexLLM
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    live_llm.install(monkeypatch)
    executor = CompletionExecutor(
        RoleBindings(
            "deepseek/deepseek-flash",
            live_llm.credentials,
            generation={"document_index": {"max_tokens": 256, "reasoning_effort": "none"}},
        ),
        ModelCallPolicy(max_calls=6, retries=0),
    )
    with llm_client_scope(PageIndexLLM(executor), required=True):
        yield executor
