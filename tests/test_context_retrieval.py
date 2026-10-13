"""Real ConDB retrieval uses shared sends and rereads pinned original bytes."""

import json
import re
import sqlite3

import pytest
from test_answer_evidence_regression import selection
from test_llm_execution import executor_for
from test_vendor_sdk import response, streamed_response

pytest_plugins = ("test_vendor_sdk",)


def test_retrieval_fits_complementary_originals_and_reduces_navigation(kb_dir, model_service):
    from openkb.config_state import capture_config
    from openkb.context.knowledge import KnowledgeRetrieval
    from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock
    from openkb.model_tokenizer import question_measure

    paragraphs = [
        f"Step {n}: preserve the complete condition and explanation. " * 15 for n in range(80)
    ]
    atomic_write_json(
        kb_dir / "wiki/sources/alpha.json",
        [{"page": 1, "unit_kind": "page", "content": "\n\n".join(paragraphs)}],
    )
    atomic_write_text(
        kb_dir / "wiki/sources/beta.md", "Beta requires the complementary second step."
    )
    executor = executor_for(model_service)
    with kb_ingest_lock(kb_dir / ".openkb"):
        executor.question_capacity = question_measure(capture_config(kb_dir))
    search = KnowledgeRetrieval(selection(kb_dir), executor)

    def rank(request):
        return rank_reply(re.findall(r"- id: (\S+)", request["messages"][-1]["content"]))

    model_service.replies.extend([(200, rank)] * 3)
    result = search.search("Give the complete steps for Alpha and Beta")
    assert result["status"] == "completed", result
    assert len(model_service.requests) == 1
    hits = {hit["doc_name"]: hit for hit in result["hits"]}
    assert hits["alpha"]["content"]
    assert paragraphs[0] in hits["alpha"]["content"]
    assert hits["alpha"]["coverage"] == "partial"
    assert hits["alpha"]["continuation"] > 0
    assert (
        hits["alpha"]["continuation_action"]["arguments"]["offset"] == hits["alpha"]["continuation"]
    )
    assert hits["beta"]["content"] == "Beta requires the complementary second step."
    assert result["question_context_tokens"] <= 6000
    repeated = search.search("Give the complete steps for Alpha and Beta")
    assert repeated["cache_hit"]
    assert len(model_service.requests) == 1
    assert all(hit["coverage"] == "already_returned" for hit in repeated["hits"])


def test_token_projection_preserves_table_rows_headers_and_continuation(kb_dir):
    from test_original_structure import frozen_text

    from openkb.agent.evidence_payload import project_evidence
    from openkb.model_tokenizer import TokenMeasure
    from openkb.text_source import read_text_selection

    measure = TokenMeasure(
        {"unknown_reason": None, "input_limit": 65536, "measurement_model": "gpt-4o-mini"}
    )
    text = (
        "# Operation under load\n\n| Object | Condition | Value |\n| --- | --- | --- |\n"
        + "".join(f"| Unit {n} | full load only | {n + 17} ms |\n" for n in range(100))
    )
    source = read_text_selection(frozen_text(kb_dir, text))
    offset, pieces = 0, []
    while True:
        hit = project_evidence(
            source, offset=offset, fits=lambda payload: measure.payload_tokens(payload) <= 700
        )
        assert hit["content"]
        assert measure.payload_tokens(hit) <= 700
        assert "| Object | Condition | Value |" in hit["content"] + str(
            hit.get("display_context", "")
        )
        pieces.append(hit["content"])
        if hit["continuation"] is None:
            break
        assert hit["continuation"] > offset
        offset = hit["continuation"]
    assert "".join(pieces) == text


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


@pytest.mark.parametrize(
    "pages",
    [
        [{"page": True, "unit_kind": "page", "content": "Invalid ordinal."}],
        [{"page": 0, "unit_kind": "page", "content": "Invalid ordinal."}],
        [{"page": 1, "unit_kind": "page", "content": None}],
        [{"page": 1, "unit_kind": "page", "content": "Duplicate."}] * 2,
    ],
)
def test_projection_rejects_corrupt_native_pages_before_retrieval(kb_dir, model_service, pages):
    from openkb.context.knowledge import KnowledgeRetrieval
    from openkb.locks import atomic_write_json

    atomic_write_json(kb_dir / "wiki/sources/broken.json", pages)
    search = KnowledgeRetrieval(selection(kb_dir), executor_for(model_service))
    with pytest.raises(ValueError, match="Stored source"):
        search.ensure_projection()
    assert model_service.requests == []
    with sqlite3.connect(kb_dir / ".openkb/context/runtime.sqlite") as db:
        assert db.execute("SELECT count(*) FROM okb_projections").fetchone()[0] == 0


@pytest.mark.parametrize("cell_size", [19610, 19615, 19617])
def test_bounded_worksheet_retrieval_does_not_cover_unread_values(
    kb_dir, tmp_path, model_service, cell_size
):
    from pathlib import Path

    from openpyxl import Workbook

    from openkb.application.documents import import_document
    from openkb.config_state import capture_config
    from openkb.context.knowledge import KnowledgeRetrieval
    from openkb.locks import atomic_write_text, kb_ingest_lock
    from openkb.model_tokenizer import question_measure

    atomic_write_text(
        kb_dir / ".env", f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n"
    )
    source = tmp_path / "large-cell.xlsx"
    book = Workbook()
    book.active["A1"] = "x" * cell_size
    book.active["A2"] = "45"
    book.save(source)
    model_service.replies.extend(
        (200, response({"role": "assistant", "content": json.dumps(value)}))
        for value in ({"description": "Sheet", "content": "Retained cells."}, {})
    )
    assert import_document(kb_dir, source).status == "added"
    captured = selection(kb_dir)
    assert any(Path(p).name.endswith(".content.json") for p in captured.views[0].files)
    executor = executor_for(model_service)
    with kb_ingest_lock(kb_dir / ".openkb"):
        executor.question_capacity = question_measure(capture_config(kb_dir))
    search = KnowledgeRetrieval(captured, executor)
    projection = search.ensure_projection()
    with sqlite3.connect(kb_dir / ".openkb/context/runtime.sqlite") as db:
        rows = db.execute(
            "SELECT node_id, attrs_json FROM nodes WHERE tree_id=?", (projection.tree_id,)
        ).fetchall()
    directories = [node for node, attrs in rows if json.loads(attrs or "{}").get("doc_name")]
    leaves = [node for node, attrs in rows if json.loads(attrs or "{}").get("source_ref")]
    original_reads = [
        search._read(json.loads(attrs)["source_ref"])
        for _, attrs in rows
        if json.loads(attrs or "{}").get("source_ref")
    ]
    assert any(hit.get("cells") for hit in original_reads)
    for hit in original_reads:
        for cell in hit.get("cells", []):
            if cell.get("value_complete") is not False:
                value = "x" * cell_size if cell["coordinate"] == "A1" else "45"
                assert f"{cell['coordinate']}: {json.dumps(value)}" in hit.get("content", "")
    model_service.replies.extend([(200, rank_reply(directories)), (200, rank_reply(leaves))])
    result = search.search("Read A1 and A2")
    assert result["status"] == "completed", result
    assert result["hits"]
    for hit in result["hits"]:
        for cell in hit.get("cells", []):
            if cell.get("value_complete") is not False:
                value = "x" * cell_size if cell["coordinate"] == "A1" else "45"
                assert f"{cell['coordinate']}: {json.dumps(value)}" in hit.get("content", "")


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
    from openkb.config_state import capture_config
    from openkb.locks import kb_ingest_lock
    from openkb.model_tokenizer import question_measure

    with kb_ingest_lock(kb_dir / ".openkb"):
        executor.question_capacity = question_measure(capture_config(kb_dir))
    search = KnowledgeRetrieval(captured, executor)
    projection = search.ensure_projection()
    assert model_service.requests == []
    with sqlite3.connect(kb_dir / ".openkb/context/runtime.sqlite") as db:
        rows = db.execute(
            "SELECT node_id, attrs_json FROM nodes WHERE tree_id=?", (projection.tree_id,)
        ).fetchall()
    leaves = [i for i, a in rows if json.loads(a or "{}").get("source_ref")]
    model_service.replies.append((200, rank_reply(leaves)))
    result = search.search("Compare slots")
    assert result["status"] == "completed", result
    assert {hit["doc_name"] for hit in result["hits"]} == {"alpha", "beta"}
    assert "Alpha requires 12 slots." in json.dumps(result)
    assert "Beta recommends 24 slots." in json.dumps(result)
    assert search.search("Compare slots")["cache_hit"] is True
    assert len(model_service.requests) == 1
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
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
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
        assert "Alpha requires 12 slots." in tools[0]
        assert "Beta recommends 24 slots." in tools[0]
        assert json.loads(tools[-1])["cache_hit"] is True
        assert all(hit["coverage"] == "already_returned" for hit in json.loads(tools[-1])["hits"])
        assert json.loads(tools[-1])["question_context_tokens"] <= 6000
        return streamed_response(
            {"role": "assistant", "content": "Alpha requires 12; Beta recommends 24."}
        )

    model_service.replies.extend((200, reply) for reply in [search_call, rank, search_call, answer])
    events = []
    context = ExecutionContext(on_event=events.append)
    result = await ask_question(kb_dir, query, context=context)
    assert result.status == "completed", result
    assert result.answer == "Alpha requires 12; Beta recommends 24."
    assert context.executor.stage_calls == {"retrieval": 1, "answer": 3}
    receipts = read_requests(kb_dir)
    assert len(receipts) == 4
    assert {r.operation for r in receipts} == {"knowledge_retrieval", "answer"}
    activities = [e["data"] for e in events if e.get("event") == "activity"]
    assert {a["stage"] for a in activities} >= {
        "preparing",
        "retrieving",
        "reading",
        "answering",
        "saving",
    }
    starts = {a["activity_id"]: a for a in activities if a["status"] == "started"}
    finishes = {a["activity_id"]: a for a in activities if a["status"] != "started"}
    assert starts.keys() == finishes.keys()
    for identity, start in starts.items():
        assert start["task_id"] and start["attempt_id"]
        assert start["parent_activity_id"] is None or start["parent_activity_id"] in starts
        assert finishes[identity]["elapsed_seconds"] >= 0
    assert any(a.get("source_count") == 2 and a.get("unit_count") == 2 for a in activities)
    assert "Alpha requires" not in json.dumps(activities)
    assert str(kb_dir) not in json.dumps(activities)


@pytest.mark.asyncio
@pytest.mark.parametrize("language,reverse", [("en", False), ("zh", True)])
async def test_one_answer_loop_retains_comparison_calculation_and_local_unknown(
    kb_dir, model_service, language, reverse
):
    from agents import set_tracing_disabled

    from openkb.application.conversations import ask_question
    from openkb.locks import atomic_write_text

    set_tracing_disabled(True)
    atomic_write_text(
        kb_dir / ".openkb/config.yaml",
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n",
    )
    atomic_write_text(
        kb_dir / ".env", f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n"
    )
    documents = [
        ("alpha", "# Alpha\n\nAt full load Alpha has 12 slots. Offline mode is unsupported."),
        ("beta", "# Beta\n\nBeta has 24 slots at full load. Drain sessions before maintenance."),
    ]
    for name, text in reversed(documents) if reverse else documents:
        atomic_write_text(kb_dir / f"wiki/sources/{name}.md", text)
    question = {
        "en": "Compare capacity and maintenance, calculate the ratio, and advise. "
        "What is the price?",
        "zh": "比较容量和维护步骤，计算容量比并给建议，价格是多少？",
    }[language]

    def call(name, arguments):
        return streamed_response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"read-{len(model_service.requests)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            },
            "tool_calls",
        )

    def rank(request):
        content = request["messages"][-1]["content"]
        ids = re.findall(r"- id: (\S+)", content)
        return rank_reply(ids[:1])

    def read_missing(request):
        tool = next(m["content"] for m in request["messages"] if m["role"] == "tool")
        assert "Alpha has 12 slots" in tool and "Beta has 24" not in tool
        return call("read_file", {"path": "sources/beta"})

    expected = {
        "en": "At full load Alpha has 12 slots and Beta 24: 24/12 = 2. "
        "Beta requires draining sessions before maintenance; Alpha does not support offline "
        "mode. For twice the stated capacity choose Beta, subject to your operating needs. "
        "The retrieved sources do not give a price; this leaves the capacity comparison intact.",
        "zh": "满载时 Alpha 有 12 个槽位，Beta 有 24 个，24/12 = 2。Beta 维护前要排空会话；"
        "Alpha 明确不支持离线模式。若目标是上述两倍容量，可按运行需求选择 Beta。"
        "已读原文没有提供价格，价格缺口不影响这个容量比较。",
    }[language]

    def answer(request):
        tools = [m["content"] for m in request["messages"] if m["role"] == "tool"]
        assert len(tools) == 2
        assert "Beta has 24 slots" in tools[-1] and "Drain sessions" in tools[-1]
        return streamed_response({"role": "assistant", "content": expected})

    model_service.replies.extend(
        (200, reply)
        for reply in [
            lambda _: call("search_knowledge", {"query": question}),
            rank,
            read_missing,
            answer,
        ]
    )
    result = await ask_question(kb_dir, question)
    assert result.status == "completed", result
    assert result.answer == expected
    assert len(model_service.requests) == 4


def test_projection_keeps_selected_publication_identity_after_source_update(kb_dir, model_service):
    import pymupdf

    from openkb.application.documents import import_document
    from openkb.application.query_views import resolve_query_views
    from openkb.application.views import view_scope
    from openkb.context.projection import knowledge_forest
    from openkb.locks import kb_ingest_lock
    from openkb.unit_publication import read_head

    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE={model_service.url}\n")

    def publish(name, text):
        path = kb_dir / name
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), text)
            path.write_bytes(pdf.tobytes())
        model_service.replies.extend(
            (200, response({"role": "assistant", "content": json.dumps(value)}))
            for value in ({"description": "Source", "content": text}, {})
        )
        result = import_document(kb_dir, path)
        assert result.status == "added", result
        return result

    first = publish("alpha.pdf", "Original Alpha requires 12 slots.")
    publish("beta.pdf", "Beta recommends 24 slots.")
    revision = read_head(kb_dir, "legacy").knowledge_revision_id
    snapshot = view_scope(kb_dir, "legacy", historical_revision=revision)
    latest = publish("alpha.pdf", "Updated Alpha requires 36 slots.")
    with kb_ingest_lock(kb_dir / ".openkb"):
        old_selection = resolve_query_views(kb_dir, "Compare slots", scope=snapshot)
        new_selection = resolve_query_views(kb_dir, "Compare slots")
        old = knowledge_forest(old_selection)
        new = knowledge_forest(new_selection)
    old_refs = [
        leaf["source_ref"]
        for doc in old["children"].values()
        for leaf in [doc, *doc.get("children", {}).values()]
        if leaf.get("source_ref", {}).get("doc_name") == "alpha"
    ]
    new_refs = [
        leaf["source_ref"]
        for doc in new["children"].values()
        for leaf in [doc, *doc.get("children", {}).values()]
        if leaf.get("source_ref", {}).get("doc_name") == "alpha"
    ]
    assert old_refs and new_refs
    assert {ref["source_revision_id"] for ref in old_refs} == {first.source_revision_id}
    assert {ref["unit_revision_id"] for ref in old_refs} == {first.units[0].successful_revision_id}
    assert {ref["source_revision_id"] for ref in new_refs} == {latest.source_revision_id}
    assert {ref["unit_revision_id"] for ref in new_refs} == {latest.units[0].successful_revision_id}
