"""Real ConDB retrieval uses shared sends and rereads pinned original bytes."""

import json
import re
import sqlite3

import pytest
from test_answer_evidence_regression import selection
from test_llm_execution import executor_for

pytest_plugins = ("test_vendor_sdk", "processing_fixtures")


def _complementary_originals(kb_dir):
    from ultrakb.locks import atomic_write_json, atomic_write_text

    paragraphs = [
        "Step 1: drain active sessions before starting Alpha maintenance.",
        "Step 2: inspect the Alpha valve only while the line is cold.",
        "Step 3: restart Alpha only after pressure has stabilised.",
    ]
    atomic_write_json(
        kb_dir / "wiki/sources/alpha.json",
        [{"page": 1, "unit_kind": "page", "content": "\n\n".join(paragraphs)}],
    )
    atomic_write_text(
        kb_dir / "wiki/sources/beta.md", "Beta requires the complementary second step."
    )
    return paragraphs


def test_retrieval_fits_complementary_originals_and_reduces_navigation(kb_dir, model_service):
    from ultrakb.config_state import capture_config
    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.context.projection import knowledge_forest
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.model_tokenizer import question_measure

    paragraphs = _complementary_originals(kb_dir)
    executor = executor_for(model_service)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        executor.question_capacity = question_measure(capture_config(kb_dir))
    search = KnowledgeRetrieval(selection(kb_dir), executor)
    navigation = knowledge_forest(search.selection)["children"]
    assert len(navigation) == 2
    assert all(node.get("source_ref") and not node.get("children") for node in navigation.values())

    result = search.search("Give the complete steps for Alpha and Beta")
    assert result["status"] == "completed", result
    assert result["coverage"] == "selected_candidates"
    assert 1 <= len(model_service.requests) <= 4
    hits = {hit["doc_name"]: hit for hit in result["hits"]}
    assert all(paragraph in hits["alpha"]["content"] for paragraph in paragraphs)
    assert hits["alpha"]["coverage"] == "complete"
    assert hits["alpha"]["continuation"] is None
    assert hits["beta"]["content"] == "Beta requires the complementary second step."
    measure = executor.question_capacity
    assert result["question_context_tokens"] <= min(6000, measure.input_limit)
    assert all(
        measure.payload_tokens(request) <= measure.input_limit for request in model_service.requests
    )
    sent = len(model_service.requests)
    repeated = search.search("Give the complete steps for Alpha and Beta")
    assert repeated["cache_hit"]
    assert len(model_service.requests) == sent
    assert all(hit["coverage"] == "already_returned" for hit in repeated["hits"])


def test_token_fit_limits_original_delivery_and_keeps_exact_continuation(kb_dir, model_service):
    from ultrakb.agent.evidence_payload import project_evidence
    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.context.projection import knowledge_forest
    from ultrakb.locks import atomic_write_json
    from ultrakb.model_tokenizer import TokenMeasure
    from ultrakb.source_pages import read_page_selection

    paragraphs = _complementary_originals(kb_dir)
    paragraphs += [
        "Step 4: confirm that Alpha's drain valve remains closed before reconnecting "
        "the cold line.",
        "Step 5: record the stabilised pressure and the operator's sign-off before returning "
        "Alpha to service.",
    ]
    atomic_write_json(
        kb_dir / "wiki/sources/alpha.json",
        [{"page": 1, "unit_kind": "page", "content": "\n\n".join(paragraphs)}],
    )
    executor = executor_for(model_service)
    measure = executor.question_capacity = TokenMeasure(
        {"unknown_reason": None, "input_limit": 65536, "measurement_model": "gpt-4o-mini"}
    )
    search = KnowledgeRetrieval(selection(kb_dir), executor)
    reference = next(
        node["source_ref"]
        for node in knowledge_forest(search.selection)["children"].values()
        if node["source_ref"]["doc_name"] == "alpha"
    )
    complete = search._read(reference)
    for token_budget in range(measure.payload_tokens(complete)):
        partial = search._read(reference, token_budget=token_budget)
        if partial["coverage"] == "partial" and paragraphs[0] in partial["content"]:
            break
    else:
        pytest.fail("No token limit can retain the complete first condition in a partial read")
    assert measure.payload_tokens(partial) <= token_budget
    assert partial["coverage"] == "partial" and partial["range_complete"] is False
    assert paragraphs[0] in partial["content"]
    assert 0 < partial["continuation"] < len(complete["content"])
    action = partial["continuation_action"]
    assert action["tool"] == "get_page_content"
    assert action["arguments"] == {
        "doc_name": "alpha",
        "pages": "1",
        "offset": partial["continuation"],
    }
    source = read_page_selection(json.loads((kb_dir / "wiki/sources/alpha.json").read_text()))
    remainder = project_evidence(source, offset=action["arguments"]["offset"])
    assert partial["content"] + remainder["content"] == complete["content"]
    assert partial["page_spans"][-1]["end"] == remainder["page_spans"][0]["start"]
    assert model_service.requests == []


def test_token_projection_preserves_table_rows_headers_and_continuation(kb_dir):
    from test_original_structure import frozen_text

    from ultrakb.agent.evidence_payload import project_evidence
    from ultrakb.model_tokenizer import TokenMeasure
    from ultrakb.text_source import read_text_selection

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
    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.locks import atomic_write_json

    atomic_write_json(kb_dir / "wiki/sources/broken.json", pages)
    search = KnowledgeRetrieval(selection(kb_dir), executor_for(model_service))
    with pytest.raises(ValueError, match="Stored source"):
        search.ensure_projection()
    assert model_service.requests == []
    with sqlite3.connect(kb_dir / ".ultrakb/context/runtime.sqlite") as db:
        assert db.execute("SELECT count(*) FROM okb_projections").fetchone()[0] == 0


@pytest.mark.parametrize("cell_size", [19610, 19615, 19617])
def test_bounded_worksheet_retrieval_does_not_cover_unread_values(
    kb_dir, tmp_path, model_service, cell_size, processing_only
):
    from pathlib import Path

    from openpyxl import Workbook

    from ultrakb.config_state import capture_config
    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.model_tokenizer import question_measure

    source = tmp_path / "large-cell.xlsx"
    book = Workbook()
    book.active["A1"] = "x" * cell_size
    book.active["A2"] = "45"
    book.save(source)
    retained = processing_only(source)
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.normalization import normalization_id, read_normalization
    from ultrakb.source_catalog import read_record
    from ultrakb.unit_publication import copy_tree, list_source_units

    [unit] = list_source_units(kb_dir, retained.source_id)
    revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
    directory, _ = read_normalization(
        kb_dir, normalization_id(retained.source_revision_id, revision.processing_fingerprint)
    )
    copy_tree(directory / "wiki", kb_dir / "wiki")
    captured = selection(kb_dir)
    assert any(Path(p).name.endswith(".content.json") for p in captured.views[0].files)
    executor = executor_for(model_service)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        executor.question_capacity = question_measure(capture_config(kb_dir))
    search = KnowledgeRetrieval(captured, executor)
    projection = search.ensure_projection()
    with sqlite3.connect(kb_dir / ".ultrakb/context/runtime.sqlite") as db:
        rows = db.execute(
            "SELECT node_id, attrs_json FROM nodes WHERE tree_id=?", (projection.tree_id,)
        ).fetchall()
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
    bounded = [
        search._read(json.loads(attrs)["source_ref"], token_budget=1200)
        for _, attrs in rows
        if json.loads(attrs or "{}").get("source_ref")
    ]
    assert bounded
    assert any(hit.get("coverage") == "budget_exceeded" for hit in bounded)
    assert all(not hit["range_complete"] for hit in bounded if hit["coverage"] == "budget_exceeded")
    for hit in bounded:
        for cell in hit.get("cells", []):
            if cell.get("value_complete") is not False:
                value = "x" * cell_size if cell["coordinate"] == "A1" else "45"
                assert f"{cell['coordinate']}: {json.dumps(value)}" in hit.get("content", "")
            else:
                assert cell.get("value") != "x" * cell_size
    assert model_service.requests == []


def test_condb_adapter_preserves_tools_and_shares_retrieval_stage_budget(kb_dir, model_service):
    from ultrakb.context.condb_llm import ConDBLLMAdapter
    from ultrakb.llm_execution import StageBudgetExceeded

    executor = executor_for(model_service, max_calls=20, retries=1, backoff=0)
    adapter = ConDBLLMAdapter(executor, "knowledge_retrieval")
    request = adapter.request(
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
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Source A"}],
            },
        ],
        tools=[{"name": "read", "input_schema": {"type": "object"}}],
        cache_key="stable",
    )
    sent = request.payload()["messages"]
    assert any(message.get("tool_call_id") == "t1" for message in sent)
    assert any(message.get("content") == "First\nSecond" for message in sent)
    assert request.payload()["cache_segments"] == [0]
    for number in range(8):
        executor.reserve("knowledge_retrieval" if number % 2 == 0 else "conversation_retrieval")
    with pytest.raises(StageBudgetExceeded):
        executor.reserve("knowledge_retrieval")
    executor.reserve("answer")
    assert executor.calls == 9
    assert executor.stage_calls == {"retrieval": 8, "answer": 1}
    assert model_service.requests == []


def test_knowledge_projection_retrieves_multiple_sources_and_invalidates_changed_bytes(
    kb_dir, model_service
):
    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.locks import atomic_write_text

    atomic_write_text(kb_dir / "wiki/sources/alpha.md", "Alpha requires 12 slots.")
    atomic_write_text(kb_dir / "wiki/sources/beta.md", "Beta recommends 24 slots.")
    captured = selection(kb_dir)
    executor = executor_for(model_service)
    from ultrakb.config_state import capture_config
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.model_tokenizer import question_measure

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        executor.question_capacity = question_measure(capture_config(kb_dir))
    search = KnowledgeRetrieval(captured, executor)
    projection = search.ensure_projection()
    assert model_service.requests == []
    with sqlite3.connect(kb_dir / ".ultrakb/context/runtime.sqlite") as db:
        rows = db.execute(
            "SELECT node_id, attrs_json FROM nodes WHERE tree_id=?", (projection.tree_id,)
        ).fetchall()
    assert any(json.loads(attrs or "{}").get("source_ref") for _, attrs in rows)
    result = search.search("Compare slots")
    assert result["status"] == "completed", result
    assert {hit["doc_name"] for hit in result["hits"]} == {"alpha", "beta"}
    assert "Alpha requires 12 slots." in json.dumps(result)
    assert "Beta recommends 24 slots." in json.dumps(result)
    sent = len(model_service.requests)
    assert 1 <= sent <= 4
    assert search.search("Compare slots")["cache_hit"] is True
    assert len(model_service.requests) == sent
    atomic_write_text(kb_dir / "wiki/sources/alpha.md", "Changed after selection")
    stale = search.search("Compare slots")
    assert stale["status"] == "evidence_rejected"
    assert "Alpha requires 12 slots." not in json.dumps(stale)


@pytest.mark.asyncio
async def test_application_uses_real_retrieval_and_reuses_tool_results(kb_dir, model_service):
    from ultrakb.application.conversations import ask_question
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.llm_usage import read_requests
    from ultrakb.locks import atomic_write_text

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n"
    )
    from agents import set_tracing_disabled

    set_tracing_disabled(True)
    atomic_write_text(kb_dir / "wiki/sources/alpha.md", "Alpha requires 12 slots.")
    atomic_write_text(kb_dir / "wiki/sources/beta.md", "Beta recommends 24 slots.")
    query = (
        "Use search_knowledge with query Compare slots twice in separate turns to verify "
        "cached original delivery, then briefly compare Alpha and Beta slots."
    )

    events = []
    context = ExecutionContext(on_event=events.append)
    result = await ask_question(kb_dir, query, context=context)
    assert result.status == "completed", result
    assert all(term in result.answer.lower() for term in ("alpha", "beta", "12", "24"))
    outputs = [
        json.loads(message["content"])
        for request in model_service.requests
        for message in request["messages"]
        if message["role"] == "tool"
        and message["content"].startswith("{")
        and "cache_hit" in message["content"]
    ]
    assert any(output["cache_hit"] for output in outputs)
    assert any(
        all(hit["coverage"] == "already_returned" for hit in output["hits"])
        for output in outputs
        if output["cache_hit"]
    )
    assert context.executor.stage_calls["retrieval"] >= 1
    assert context.executor.stage_calls["answer"] >= 2
    receipts = read_requests(kb_dir)
    assert len(receipts) == len(model_service.requests)
    assert 3 <= len(receipts) <= 10
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

    from ultrakb.application.conversations import ask_question
    from ultrakb.locks import atomic_write_text

    set_tracing_disabled(True)
    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n",
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

    result = await ask_question(kb_dir, question)
    assert result.status == "completed", result
    answer = result.answer.lower()
    assert all(term in answer for term in ("alpha", "beta", "12", "24"))
    assert re.search(r"2\b|twice|两倍", answer)
    assert any(term in answer for term in ("drain", "排空", "清空"))
    assert any(term in answer for term in ("offline", "离线"))
    assert any(term in answer for term in ("not", "unknown", "未", "没有", "不"))
    delivered = json.dumps(
        [
            m["content"]
            for r in model_service.requests
            for m in r["messages"]
            if m["role"] == "tool"
        ],
        ensure_ascii=False,
    )
    assert "Alpha has 12 slots" in delivered and "Beta has 24 slots" in delivered
    assert "Drain sessions" in delivered and "Offline mode is unsupported" in delivered
    assert 2 <= len(model_service.requests) <= 10


def test_projection_keeps_selected_publication_identity_after_source_update(kb_dir, model_service):
    import pymupdf
    from publication_fixtures import publish_document

    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.views import view_scope
    from ultrakb.context.projection import knowledge_forest
    from ultrakb.locks import atomic_write_bytes, atomic_write_text, kb_ingest_lock
    from ultrakb.unit_publication import read_head

    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n",
    )

    def publish(name, text):
        path = kb_dir / name
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), text)
            atomic_write_bytes(path, pdf.tobytes())
        result = publish_document(kb_dir, path, text, bundle=model_service.credentials)
        assert result.status == "added", result
        return result

    first = publish("alpha.pdf", "Original Alpha requires 12 slots.")
    publish("beta.pdf", "Beta recommends 24 slots.")
    revision = read_head(kb_dir, "legacy").knowledge_revision_id
    snapshot = view_scope(kb_dir, "legacy", historical_revision=revision)
    latest = publish("alpha.pdf", "Updated Alpha requires 36 slots.")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
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
    assert model_service.requests == []
