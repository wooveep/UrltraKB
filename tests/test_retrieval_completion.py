"""Real ConDB responses must complete before their rank arguments can be evidence."""

import pytest
from test_context_retrieval import rank_reply
from test_llm_execution import executor_for
from test_vendor_sdk import response

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
    from openkb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure

    captured = rank_reply(["valid"]) if has_rank else response()
    captured["choices"][0]["finish_reason"] = "length"
    if has_rank:
        captured["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            '{"ranked_ids": ['
        )
    model_service.replies.extend([(200, captured)] * 2)
    executor = executor_for(model_service)
    adapter = ConDBLLMAdapter(executor, role)
    with pytest.raises(RetrievalFailure) as raised:
        adapter.chat([{"role": "user", "content": "Same frozen question and candidates"}])
    assert raised.value.code == "output_truncated"
    assert len(model_service.requests) == 2
    assert model_service.requests[0]["messages"] == model_service.requests[1]["messages"]
    assert model_service.requests[1]["max_tokens"] == 8192


@pytest.mark.parametrize("role", ["knowledge_retrieval", "conversation_retrieval"])
@pytest.mark.parametrize(
    "kind", ["valid", "empty", "missing", "json", "shape", "outside", "no_done"]
)
def test_completed_rank_protocol_keeps_distinct_failure_reasons(model_service, role, kind):
    from openkb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure

    reply = rank_reply([] if kind == "empty" else ["outside" if kind == "outside" else "valid"])
    reply["choices"][0]["finish_reason"] = "stop"
    if kind == "missing":
        reply = response()
    elif kind in {"json", "shape"}:
        reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            "{" if kind == "json" else '{"ranked_ids": "valid"}'
        )
    elif kind == "no_done":
        reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            '{"ranked_ids": ["valid"]}'
        )
    model_service.replies.append((200, reply))
    adapter = ConDBLLMAdapter(executor_for(model_service, retrieval_repairs=0), role)
    if kind in {"valid", "empty"}:
        result = adapter.chat([{"role": "user", "content": "Rank"}], tools=RANK_TOOLS)
        assert result["content"][0]["input"]["ranked_ids"] == ([] if kind == "empty" else ["valid"])
    else:
        with pytest.raises(RetrievalFailure) as raised:
            adapter.chat([{"role": "user", "content": "Rank"}], tools=RANK_TOOLS)
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


def test_exhausted_failure_is_cached_for_the_current_question(model_service, kb_dir):
    from test_answer_evidence_regression import selection

    from openkb.context.knowledge import KnowledgeRetrieval

    (kb_dir / "wiki/sources/note.md").write_text("Instructions for pruning roses.")
    truncated = response(reason="length")
    model_service.replies.extend([(200, truncated)] * 3)
    executor = executor_for(model_service)
    search = KnowledgeRetrieval(selection(kb_dir), executor)
    first = search.search("When should roses be pruned?")
    second = search.search("When should roses be pruned?")
    assert first["reason_code"] == second["reason_code"] == "output_truncated"
    assert first["status"] == second["status"] == "degraded"
    assert first["consumed_sends"] == second["consumed_sends"] == 2
    assert second["cache_hit"]
    assert len(model_service.requests) == 2
    search.search("How much should I prune?")
    assert len(model_service.requests) > 2


def test_logical_repairs_and_transport_retries_share_eight_sends(kb_dir, model_service):
    from openkb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure
    from openkb.llm_execution import StageBudgetExceeded
    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution

    model_service.replies.extend(
        [(503, {"error": {"message": "busy"}})] + [(200, response(reason="length"))] * 7
    )
    executor = executor_for(model_service, retries=1, backoff=0)
    with import_usage_execution(kb_dir, "retrieval"):
        for i in range(5):
            adapter = ConDBLLMAdapter(
                executor, "knowledge_retrieval" if i % 2 == 0 else "conversation_retrieval"
            )
            with pytest.raises(RetrievalFailure, match="output_truncated"):
                adapter.chat([{"role": "user", "content": f"Rank step {i}"}])
        with pytest.raises(StageBudgetExceeded):
            adapter.chat([{"role": "user", "content": "No allowance"}])
    assert executor.calls == len(model_service.requests) == len(read_requests(kb_dir)) == 8
    assert [request["max_tokens"] for request in model_service.requests].count(8192) == 2


def test_larger_output_must_fit_captured_context(model_service):
    from openkb.context.condb_llm import ConDBLLMAdapter, RetrievalFailure
    from openkb.model_tokenizer import TokenMeasure

    executor = executor_for(model_service)
    executor.question_capacity = TokenMeasure(
        {
            "input_limit": 5000,
            "measurement_model": "gpt-4o-mini",
            "unknown_reason": None,
            "context_window_tokens": 6000,
        }
    )
    model_service.replies.append((200, response(reason="length")))
    with pytest.raises(RetrievalFailure, match="retrieval_context_capacity"):
        ConDBLLMAdapter(executor, "knowledge_retrieval").chat(
            [{"role": "user", "content": "Question"}]
        )
    assert len(model_service.requests) == 1


def test_frozen_retrieval_policy_changes_cache_identity(model_service):
    baseline = executor_for(model_service)
    candidate = executor_for(model_service, retrieval_tokens=4096)
    assert baseline.fingerprint != candidate.fingerprint
