"""Real ConDB retrieval uses shared sends and rereads pinned original bytes."""

import json
import re
import sqlite3

import pytest
from test_answer_evidence_regression import selection
from test_llm_execution import executor_for
from test_vendor_sdk import response, streamed_response

pytest_plugins = ("test_vendor_sdk",)


def rank_reply(ids):
    return response(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "rank-1",
                    "type": "function",
                    "function": {
                        "name": "rank",
                        "arguments": json.dumps({"ranked_ids": ids, "done": False}),
                    },
                }
            ],
        },
        reason="tool_calls",
    )


def test_condb_adapter_preserves_tools_and_shares_retry_stage_budget(kb_dir, model_service):
    from openkb.context.condb_llm import ConDBLLMAdapter
    from openkb.llm_execution import ModelRequest, StageBudgetExceeded
    from openkb.llm_usage import read_requests
    from openkb.llm_usage_execution import import_usage_execution

    executor = executor_for(model_service, max_calls=20, retries=1, backoff=0)
    with import_usage_execution(kb_dir, "query"):
        adapter = ConDBLLMAdapter(executor, "knowledge_retrieval")
        model_service.replies.extend([(503, {"error": {"message": "busy"}}), (200, response())])
        result = adapter.chat_with_cache(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "First"},
                        {"type": "text", "text": "Second"},
                        {"type": "tool_use", "id": "t1", "name": "read", "input": {"id": "a"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "Source A"}
                    ],
                },
            ],
            tools=[{"name": "read", "input_schema": {"type": "object"}}],
            cache_content="Stable navigation",
            non_cached_content="New question",
        )
        assert result["content"] == [{"type": "text", "text": "ok"}]
        sent = model_service.requests[-1]["messages"]
        assert any(m.get("tool_call_id") == "t1" for m in sent)
        assert any(m.get("content") == "First\nSecond" for m in sent)
        model_service.replies.extend([(200, response())] * 7)
        other = ConDBLLMAdapter(executor, "conversation_retrieval")
        for _ in range(6):
            other.chat([{"role": "user", "content": "next"}])
        with pytest.raises(StageBudgetExceeded):
            adapter.chat([{"role": "user", "content": "excess"}])
        executor.complete(
            "answer",
            ModelRequest(
                [{"role": "user", "content": "answer"}], operation="query", stage="answer"
            ),
        )
    assert len(model_service.requests) == executor.calls == len(read_requests(kb_dir)) == 9


def test_knowledge_projection_retrieves_multiple_sources_and_invalidates_changed_bytes(
    kb_dir, model_service
):
    from openkb.context.knowledge import KnowledgeRetrieval

    (kb_dir / "wiki/sources/alpha.md").write_text("Alpha requires 12 slots.")
    (kb_dir / "wiki/sources/beta.md").write_text("Beta recommends 24 slots.")
    captured = selection(kb_dir)
    executor = executor_for(model_service)
    search = KnowledgeRetrieval(captured, executor)
    projection = search.ensure_projection()
    assert model_service.requests == []
    with sqlite3.connect(kb_dir / ".openkb/context/runtime.sqlite") as db:
        rows = db.execute(
            "SELECT node_id, attrs_json FROM nodes WHERE tree_id=?", (projection.tree_id,)
        ).fetchall()
    directories = [
        i
        for i, a in rows
        if json.loads(a or "{}").get("is_dir") and json.loads(a or "{}").get("doc_name")
    ]
    leaves = [i for i, a in rows if json.loads(a or "{}").get("source_ref")]
    model_service.replies.extend([(200, rank_reply(directories)), (200, rank_reply(leaves))])
    result = search.search("Compare slots")
    assert result["status"] == "completed", result
    assert {hit["doc_name"] for hit in result["hits"]} == {"alpha", "beta"}
    assert "Alpha requires 12 slots." in json.dumps(result)
    assert "Beta recommends 24 slots." in json.dumps(result)
    assert search.search("Compare slots")["cache_hit"] is True
    assert len(model_service.requests) == 2
    (kb_dir / "wiki/sources/alpha.md").write_text("Changed after selection")
    stale = search.search("Compare slots")
    assert stale["status"] == "evidence_rejected"
    assert "Alpha requires 12 slots." not in json.dumps(stale)


@pytest.mark.asyncio
async def test_application_uses_real_retrieval_and_reuses_tool_results(kb_dir, model_service):
    from openkb.application.conversations import ask_question
    from openkb.application.execution import ExecutionContext
    from openkb.llm_usage import read_requests
    from openkb.locks import atomic_write_text

    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")
    from agents import set_tracing_disabled

    set_tracing_disabled(True)
    atomic_write_text(kb_dir / "wiki/sources/alpha.md", "Alpha requires 12 slots.")
    atomic_write_text(kb_dir / "wiki/sources/beta.md", "Beta recommends 24 slots.")
    query = "Compare slots"

    def search_call(request):
        tool = next(
            t["function"] for t in request["tools"] if t["function"]["name"] == "search_knowledge"
        )
        assert set(tool["parameters"]["properties"]) == {"query"}
        return streamed_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"search-{len(model_service.requests)}",
                        "type": "function",
                        "function": {
                            "name": "search_knowledge",
                            "arguments": json.dumps({"query": query}),
                        },
                    }
                ],
            },
            "tool_calls",
        )

    def rank(request):
        ids = re.findall(r"- id: (\S+)", request["messages"][-1]["content"])
        assert ids
        return rank_reply(ids)

    def answer(request):
        tools = [m["content"] for m in request["messages"] if m["role"] == "tool"]
        assert len(tools) == 2
        assert "Alpha requires 12 slots." in tools[-1]
        assert "Beta recommends 24 slots." in tools[-1]
        assert json.loads(tools[-1])["cache_hit"] is True
        return streamed_response(
            {"role": "assistant", "content": "Alpha requires 12; Beta recommends 24."}
        )

    model_service.replies.extend(
        (200, reply) for reply in [search_call, rank, rank, search_call, answer]
    )
    context = ExecutionContext()
    result = await ask_question(kb_dir, query, context=context)
    assert result.status == "completed", result
    assert result.answer == "Alpha requires 12; Beta recommends 24."
    assert context.executor.stage_calls == {"retrieval": 2}
    receipts = read_requests(kb_dir)
    assert len(receipts) == 5
    assert {r.operation for r in receipts} == {"knowledge_retrieval", "answer"}
