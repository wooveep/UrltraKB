"""Parallel original reads return independent evidence to the same conversation."""

import asyncio
import json
import threading

import pytest
from agents.tool_context import ToolContext

from openkb.agent.source_tools import source_tools
from openkb.application.documents import import_document
from openkb.application.source_history import source_status


@pytest.mark.parametrize("operation", ["search_sources", "read_source_nodes"])
def test_batch_reads_overlap_and_keep_original_bindings(
    kb_dir, tmp_path, model_service, monkeypatch, operation
):
    sources = []
    for index, text in enumerate(("自动分区后删除 swap。", "手动分区有单独的磁盘条件。")):
        path = tmp_path / f"partition-{index}.md"
        path.write_text(text)
        result = import_document(kb_dir, path)
        assert result.knowledge_compilation == "completed", result
        nav = source_status(kb_dir, result.source_id)["navigation"]
        sources.append((result.source_id, nav["nodes"][0]["id"], text))
    catalog = {tool.name: tool for tool in source_tools(kb_dir)[0]}
    assert operation in catalog
    from openkb.agent import source_tools as module

    original = module.original_window
    barrier = threading.Barrier(2, timeout=3)
    threads = set()

    def read(*args, **kwargs):
        threads.add(threading.get_ident())
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "original_window", read)
    requests = [
        {"source_id": sid, "query": "分区"}
        if operation == "search_sources"
        else {"source_id": sid, "node_id": node}
        for sid, node, _ in sources
    ]
    arguments = json.dumps({"searches" if operation == "search_sources" else "reads": requests})
    output = asyncio.run(
        catalog[operation].on_invoke_tool(
            ToolContext(
                context=None, tool_name=operation, tool_call_id="batch", tool_arguments=arguments
            ),
            arguments,
        )
    )
    rows = json.loads(output)["results"]
    assert len(threads) == 2
    assert [row["request"]["source_id"] for row in rows] == [s[0] for s in sources]
    for row, (sid, _, text) in zip(rows, sources, strict=True):
        assert "error" not in row
        evidence = row["output"]["evidence"][0]
        assert evidence["reference"]["source_id"] == sid
        assert evidence["text"] == text
        assert evidence["short_citation"].startswith("[evidence:")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search_sources", "read_source_nodes"])
async def test_batch_citations_and_evidence_survive_a_followup(
    kb_dir, tmp_path, model_service, operation
):
    from openkb.agent.chat_session import load_session
    from openkb.application.conversations import continue_conversation

    requests = []
    for index, text in enumerate(("单盘自动分区后删除 swap。", "多盘手工分区不创建 swap。")):
        path = tmp_path / f"scenario-{index}.md"
        path.write_text(text)
        result = await asyncio.to_thread(import_document, kb_dir, path)
        nav = source_status(kb_dir, result.source_id)["navigation"]
        requests.append(
            {"source_id": result.source_id, "query": "swap"}
            if operation == "search_sources"
            else {"source_id": result.source_id, "node_id": nav["nodes"][0]["id"]}
        )
    model_service.clear()
    observed = []

    def chat(body):
        outputs = [row for row in body["messages"] if row["role"] == "tool"]
        if outputs:
            results = json.loads(outputs[-1]["content"])["results"]
            evidence = [row["output"]["evidence"][0] for row in results]
            assert len({row["reference"]["source_id"] for row in evidence}) == 2
            observed[:] = evidence
            return {
                "role": "assistant",
                "content": "\n".join(row["text"] + " " + row["short_citation"] for row in evidence),
            }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "batch",
                    "type": "function",
                    "function": {
                        "name": operation,
                        "arguments": json.dumps(
                            {
                                "searches" if operation == "search_sources" else "reads": requests,
                            }
                        ),
                    },
                }
            ],
        }

    model_service.chat_response = chat
    first = await continue_conversation(kb_dir, "单盘和多盘分别如何处理 swap？")
    assert first.status == "completed", first
    assert len(model_service) == 2
    second = await continue_conversation(kb_dir, "重述上述两种情况。", session_id=first.session_id)
    assert second.status == "completed", second
    assert len(model_service) == 3
    for row in observed:
        assert row["text"] in second.answer and row["citation"] in second.answer
    saved = load_session(kb_dir, second.session_id)
    assert saved.turn_count == 2
    assert saved.assistant_texts == [first.answer, second.answer]
    assert "[evidence:" not in second.answer


@pytest.mark.asyncio
async def test_batch_bounds_and_partial_failure_are_explicit():
    from openkb.agent.source_batch import batch_source_tools

    seen = []

    def read(**args):
        seen.append(args["source_id"])
        if args["source_id"] == "missing":
            raise ValueError("Source is not in this published snapshot")
        return json.dumps({"evidence": [{"text": "original"}], "next": None})

    tool = batch_source_tools(read, read)[0]

    async def invoke(reads):
        args = json.dumps({"reads": reads})
        return await tool.on_invoke_tool(
            ToolContext(
                context=None,
                tool_name=tool.name,
                tool_call_id="batch",
                tool_arguments=args,
            ),
            args,
        )

    requests = [{"source_id": sid, "node_id": "range"} for sid in ["present", "missing"]]
    output = json.loads(await invoke(requests))["results"]
    assert output[0]["output"]["evidence"][0]["text"] == "original"
    assert "snapshot" in output[1]["error"] and "output" not in output[1]
    seen.clear()
    for invalid in [[], requests * 3, [{**requests[0], "max_chars": 16000}] * 2]:
        response = await invoke(invalid)
        assert "error" in response.lower()
        assert not seen


@pytest.mark.asyncio
async def test_batch_cancellation_reaches_the_owner_without_waiting_for_readers():
    from contextvars import ContextVar

    from openkb.agent.source_batch import batch_source_tools

    context = ContextVar("source_read_test_context", default=None)
    context.set("same-question")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read(**args):
        assert context.get() == "same-question"
        started.set()
        try:
            assert release.wait(3)
            return json.dumps({"evidence": []})
        finally:
            finished.set()

    tool = batch_source_tools(read, read)[0]
    arguments = json.dumps({"reads": [{"source_id": "source", "node_id": "range"}]})
    task = asyncio.create_task(
        tool.on_invoke_tool(
            ToolContext(
                context=None,
                tool_name=tool.name,
                tool_call_id="batch",
                tool_arguments=arguments,
            ),
            arguments,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)


@pytest.mark.asyncio
async def test_batch_propagates_task_interruption_instead_of_source_error():
    from openkb.agent.source_batch import batch_source_tools
    from openkb.processing import ProcessingIncomplete

    def read(**args):
        raise ProcessingIncomplete("request_budget_exhausted", "answering")

    tool = batch_source_tools(read, read)[0]
    arguments = json.dumps({"reads": [{"source_id": "source", "node_id": "range"}]})
    with pytest.raises(ProcessingIncomplete):
        await tool.on_invoke_tool(
            ToolContext(
                context=None,
                tool_name=tool.name,
                tool_call_id="batch",
                tool_arguments=arguments,
            ),
            arguments,
        )
