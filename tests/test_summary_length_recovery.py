"""Bounded document-summary recovery keeps native coverage and task consumption."""

import json

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


def truncated_reply(request):
    reply = response({"role": "assistant", "content": ""})
    reply["choices"][0]["finish_reason"] = "length"
    return reply


def complete_reply(request):
    batch = json.loads(request["messages"][1]["content"].split("\n", 1)[1])
    return response(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "description": "Operating conditions",
                    "sections": [
                        {
                            "id": section["id"],
                            "content": batch["content"][section["start"] : section["end"]],
                        }
                        for section in batch["output_sections"]
                    ],
                }
            ),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["text", "pages"])
@pytest.mark.parametrize("stop_at", ["completed_child", "split_preparation", "received_repair"])
async def test_split_summary_resumes_completed_child_with_original_task_allowance(
    kb_dir, model_service, source_kind, stop_at, monkeypatch
):
    from openkb.agent.compiler import compile_long_doc
    from openkb.application.execution import ExecutionContext
    from openkb.locks import LockCancelled, kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  max_calls: 5\n  retries: 0\n"
    )
    original = (
        "# Start\n\n"
        + "Keep the inlet closed.\n\n" * 200
        + "# Tail\n\nOnly reopen after inspection."
    )
    suffix = "md" if source_kind == "text" else "json"
    (kb_dir / f"wiki/sources/manual.{suffix}").write_text(
        original
        if source_kind == "text"
        else json.dumps([{"page": 1, "unit_kind": "page", "content": original}])
    )
    summary = kb_dir / "wiki/summaries/manual.md"
    summary.write_text(f"---\nfull_text: sources/manual.{suffix}\n---\nPrevious successful result.")
    before = summary.read_bytes()
    from openkb.agent.source_windows import CompileSourceWindows

    actual_split = CompileSourceWindows.split_document_window
    import litellm

    actual_completion = litellm.acompletion
    stopped_after_response = False

    async def cancel_received(**kwargs):
        nonlocal stopped_after_response
        result = await actual_completion(**kwargs)
        if len(model_service.requests) == 2:
            stopped_after_response = True
        return result

    if stop_at == "received_repair":
        monkeypatch.setattr(litellm, "acompletion", cancel_received)

    def cancel_split(self, window):
        raise LockCancelled("Stop after truncated repair before split checkpoint")

    if stop_at == "split_preparation":
        monkeypatch.setattr(CompileSourceWindows, "split_document_window", cancel_split)

    def interrupted():
        return (
            stopped_after_response
            or stop_at == "completed_child"
            and any(
                record.get("entries", {}).get("summary:1.1", {}).get("state") == "accepted"
                for path in (kb_dir / ".openkb/compile-checkpoints").rglob("manifest.json")
                for record in [json.loads(path.read_text())]
            )
        )

    model_service.replies.extend(
        [(200, truncated_reply)] * 2
        + [(200, complete_reply)] * 2
        + [(200, response({"role": "assistant", "content": "{}"}))]
    )
    first = ExecutionContext(cancelled=interrupted)
    with kb_ingest_lock(kb_dir / ".openkb"), first.begin(kb_dir):
        with pytest.raises(LockCancelled):
            await compile_long_doc("manual", summary, "id", kb_dir, "ignored", navigation=[])
    assert (
        len(model_service.requests)
        == first.executor.calls
        == (3 if stop_at == "completed_child" else 2)
    )
    assert summary.read_bytes() == before
    monkeypatch.setattr(CompileSourceWindows, "split_document_window", actual_split)
    monkeypatch.setattr(litellm, "acompletion", actual_completion)
    resumed = ExecutionContext(resume_model_task_id=first.model_task.record.task_id)
    with kb_ingest_lock(kb_dir / ".openkb"), resumed.begin(kb_dir):
        await compile_long_doc("manual", summary, "id", kb_dir, "ignored", navigation=[])
    assert len(model_service.requests) == resumed.executor.calls == 5
    children = [
        json.loads(request["messages"][1]["content"].split("\n", 1)[1])
        for request in model_service.requests[2:4]
    ]
    expected = original if source_kind == "text" else "[Physical page 1]\n\n" + original
    assert "".join(child["content"] for child in children) == expected
    assert children[0]["selection_offset"] == 0
    assert children[1]["selection_offset"] == len(children[0]["content"])
    if source_kind == "text":
        assert children[0]["source_spans"][-1][1] == children[1]["source_spans"][0][0]
        assert children[-1]["source_spans"][-1][1] == len(original)
    else:
        assert children[0]["page_spans"][-1]["end"] == children[1]["page_spans"][0]["start"]
        assert children[-1]["page_spans"][-1]["end"] == len(original)
    assert all(c["primary"] is False for child in children for c in child["supplementary_context"])
    assert "Only reopen after inspection." in summary.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["child_length", "parent_protocol", "allowance"])
async def test_summary_recovery_stops_without_another_split_or_fresh_allowance(
    kb_dir, model_service, failure
):
    from openkb.agent.compiler import compile_long_doc
    from openkb.application.execution import ExecutionContext
    from openkb.compilation_report import CompilationIncomplete
    from openkb.llm_execution import ModelBudgetExceeded
    from openkb.locks import kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    limit = 4 if failure == "allowance" else 8
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n"
        f"  max_calls: {limit}\n  retries: 0\n"
    )
    (kb_dir / "wiki/sources/manual.md").write_text("Keep the inlet closed.\n\n" * 100)
    summary = kb_dir / "wiki/summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nPrevious successful result.")
    before = summary.read_bytes()
    reply = (
        response({"role": "assistant", "content": "{}"})
        if failure == "parent_protocol"
        else truncated_reply
    )
    model_service.replies.extend([(200, reply)] * 8)
    error = ModelBudgetExceeded if failure == "allowance" else CompilationIncomplete
    first = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), first.begin(kb_dir):
        with pytest.raises(error):
            await compile_long_doc("manual", summary, "id", kb_dir, "ignored", navigation=[])
    sends = 4 if failure == "child_length" else 2
    assert len(model_service.requests) == first.executor.calls == sends
    assert summary.read_bytes() == before
    resumed = ExecutionContext(resume_model_task_id=first.model_task.record.task_id)
    with kb_ingest_lock(kb_dir / ".openkb"), resumed.begin(kb_dir):
        with pytest.raises(error):
            await compile_long_doc("manual", summary, "id", kb_dir, "ignored", navigation=[])
    assert len(model_service.requests) == resumed.executor.calls == sends
    assert summary.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,limit,generation",
    [
        ("openai/gpt-4o-mini", 5000, ""),
        (
            "deepseek/deepseek-chat",
            4995,
            "role_generation:\n  compile:\n    reasoning_effort: none\n",
        ),
    ],
)
async def test_summary_window_admits_its_length_repair_before_using_the_allowance(
    kb_dir, model_service, model, limit, generation
):
    from openkb.agent.compiler import compile_long_doc
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        f"model: {model}\nlanguage: en\nmodel_capacity:\n"
        f"  max_input_tokens: {limit}\n  tokenizer_model: gpt-4o-mini\n"
        "model_policy:\n  max_calls: 20\n  retries: 0\n" + generation
    )
    original = (
        "Operating context must be reviewed before continuing.\n\n" * 3000
        if model.startswith("deepseek/")
        else "The operating condition must be retained.\n\n" * 1800
        + "Only reopen after inspection."
    )
    (kb_dir / "wiki/sources/manual.md").write_text(original)
    summary = kb_dir / "wiki/summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nPrevious result.")

    def reply(request):
        if "Based on the summary above" in str(request):
            return response({"role": "assistant", "content": "{}"})
        return complete_reply(request)

    model_service.replies.extend([(200, truncated_reply)] + [(200, reply)] * 19)
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        await compile_long_doc("manual", summary, "id", kb_dir, "ignored", navigation=[])
    original_messages = model_service.requests[0]["messages"]
    repair_messages = model_service.requests[1]["messages"]
    assert len(repair_messages) == len(original_messages) + 1
    assert repair_messages[:-1] == original_messages
    assert "failed validation (page_truncated_response)" in str(model_service.requests[1])
    assert context.executor.calls == len(model_service.requests)
    assert original.strip().splitlines()[-1] in summary.read_text()
