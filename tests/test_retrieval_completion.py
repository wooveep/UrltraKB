"""Completed retrieval parsing and bounded request planning are transport-independent."""

import json

import pytest
from test_llm_execution import executor_for

pytest_plugins = ("test_vendor_sdk",)

RANK_TOOLS = [
    {
        "name": "rank",
        "input_schema": {
            "type": "object",
            "properties": {
                "ranked_ids": {"type": "array", "items": {"type": "string", "enum": ["valid"]}},
                "done": {"type": "boolean"},
            },
        },
    }
]


@pytest.mark.parametrize("role", ["knowledge_retrieval", "conversation_retrieval"])
@pytest.mark.parametrize("has_rank", [False, True])
def test_truncated_response_is_classified_before_rank_conversion(model_service, role, has_rank):
    from ultrakb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure, rank_arguments

    calls = [{"name": "rank", "arguments": '{"ranked_ids": ['}] if has_rank else []
    with pytest.raises(RetrievalFailure) as raised:
        rank_arguments("length", calls, RANK_TOOLS)
    assert raised.value.code == "output_truncated"
    adapter = ConDBLLMAdapter(executor_for(model_service), role)
    messages = [{"role": "user", "content": "Same frozen question and candidates"}]
    initial, repair = adapter.request(messages), adapter.request(messages, attempt=1)
    assert initial.payload()["messages"] == repair.payload()["messages"]
    assert repair.payload()["options"]["max_tokens"] == 8192
    assert model_service.requests == []


@pytest.mark.parametrize("role", ["knowledge_retrieval", "conversation_retrieval"])
@pytest.mark.parametrize(
    "kind", ["valid", "empty", "missing", "json", "shape", "outside", "no_done"]
)
def test_completed_rank_protocol_keeps_distinct_failure_reasons(model_service, role, kind):
    from ultrakb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure, rank_arguments

    ids = [] if kind == "empty" else ["outside" if kind == "outside" else "valid"]
    arguments = json.dumps({"ranked_ids": ids, "done": True})
    if kind == "json":
        arguments = "{"
    elif kind == "shape":
        arguments = '{"ranked_ids": "valid"}'
    elif kind == "no_done":
        arguments = '{"ranked_ids": ["valid"]}'
    calls = [] if kind == "missing" else [{"name": "rank", "arguments": arguments}]
    prepared = ConDBLLMAdapter(executor_for(model_service), role).request(
        [{"role": "user", "content": "Rank"}], tools=RANK_TOOLS
    )
    assert prepared.payload()["tools"][0]["function"]["parameters"] == RANK_TOOLS[0]["input_schema"]
    if kind in {"valid", "empty"}:
        assert rank_arguments("stop", calls, RANK_TOOLS)["ranked_ids"] == ids
    else:
        with pytest.raises(RetrievalFailure) as raised:
            rank_arguments("stop", calls, RANK_TOOLS)
        assert (
            raised.value.code
            == {
                "missing": "rank_missing",
                "json": "rank_invalid_json",
                "shape": "rank_invalid",
                "outside": "candidate_outside_selection",
                "no_done": "rank_invalid",
            }[kind]
        )
    assert model_service.requests == []


def test_exhausted_failure_is_cached_for_the_current_question(model_service, kb_dir):
    from test_answer_evidence_regression import selection

    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.locks import atomic_write_text
    from ultrakb.model_tokenizer import TokenMeasure

    atomic_write_text(kb_dir / "wiki/sources/note.md", "Instructions for pruning roses.")
    executor = executor_for(model_service)
    executor.question_capacity = TokenMeasure(
        {
            "input_limit": 100,
            "context_window_tokens": 100,
            "measurement_model": "gpt-4o-mini",
            "unknown_reason": None,
        }
    )
    search = KnowledgeRetrieval(selection(kb_dir), executor)
    first = search.search("When should roses be pruned?")
    second = search.search("When should roses be pruned?")
    assert first["reason_code"] == second["reason_code"] == "retrieval_context_capacity"
    assert first["status"] == second["status"] == "degraded"
    assert first["consumed_sends"] == second["consumed_sends"] == 0
    assert not first["cache_hit"] and second["cache_hit"]
    distinct = search.search("How much should I prune?")
    assert not distinct["cache_hit"]
    assert model_service.requests == []


def test_logical_repairs_and_retrieval_roles_share_eight_admissions(kb_dir, model_service):
    from ultrakb.llm_execution import StageBudgetExceeded

    executor = executor_for(model_service, retries=1, backoff=0)
    assert executor.allow_retrieval_repair()
    assert executor.allow_retrieval_repair()
    assert not executor.allow_retrieval_repair()
    for index in range(8):
        executor.reserve("knowledge_retrieval" if index % 2 == 0 else "conversation_retrieval")
    with pytest.raises(StageBudgetExceeded):
        executor.reserve("knowledge_retrieval")
    assert executor.calls == 8
    assert executor.stage_calls == {"retrieval": 8}
    assert not executor.allow_retrieval_repair()
    assert model_service.requests == []


def test_larger_output_must_fit_captured_context(model_service):
    from ultrakb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure
    from ultrakb.model_tokenizer import TokenMeasure

    executor = executor_for(model_service)
    executor.question_capacity = TokenMeasure(
        {
            "input_limit": 5000,
            "measurement_model": "gpt-4o-mini",
            "unknown_reason": None,
            "context_window_tokens": 6000,
        }
    )
    adapter = ConDBLLMAdapter(executor, "knowledge_retrieval")
    messages = [{"role": "user", "content": "Question"}]
    assert adapter.request(messages).payload()["options"]["max_tokens"] == 1024
    with pytest.raises(RetrievalFailure, match="retrieval_context_capacity"):
        adapter.request(messages, attempt=1)
    assert model_service.requests == []


def test_frozen_retrieval_policy_changes_cache_identity(model_service):
    baseline = executor_for(model_service)
    candidate = executor_for(model_service, retrieval_tokens=4096)
    assert baseline.fingerprint != candidate.fingerprint
