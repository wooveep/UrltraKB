"""Long summaries retain document knowledge independently of topic selection."""

import json
from types import SimpleNamespace

import pytest
from compile_responses import summary_reply

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["pages", "text"])
async def test_empty_topic_plan_still_summarizes_every_original_unit(
    kb_dir, monkeypatch, source_kind
):
    from openkb import frontmatter
    from openkb.agent.compiler import compile_long_doc

    wiki = kb_dir / "wiki"
    facts = [f"独有要求 {i}: 执行检查后才可使用。" for i in range(1, 14)]
    suffix = "json" if source_kind == "pages" else "md"
    source = wiki / f"sources/handbook.{suffix}"
    source.write_text(
        json.dumps(
            [{"page": i, "unit_kind": "page", "content": fact} for i, fact in enumerate(facts, 1)],
            ensure_ascii=False,
        )
        if source_kind == "pages"
        else "\n\n".join(facts)
    )
    before = source.read_bytes()
    summary = wiki / "summaries/handbook.md"
    summary.write_text(
        f"---\ntype: Summary\nunit_kind: {source_kind}\n"
        f"full_text: sources/handbook.{suffix}\ncustom_field: retained\n---\n"
        "STALE_GENERATED_KNOWLEDGE\n[[concepts/missing]]"
    )
    sends = []

    def response(**kwargs):
        sends.append(kwargs["messages"])
        if "Based on the summary above" in str(kwargs["messages"]):
            reply = {}
        else:
            reply = {"description": "操作要求", "content": "# 操作要求\n\n" + "\n\n".join(facts)}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(summary_reply(kwargs["messages"], reply))
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.completion", response)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    await compile_long_doc("handbook", summary, "handbook-id", kb_dir, "test-model", navigation=[])
    original_sends = [s for s in sends if "Based on the summary above" not in str(s)]
    assert len(original_sends) == 1  # Many short units fit one request.
    prompt = str(original_sends[0])
    assert all(fact in prompt for fact in facts)
    assert "STALE_GENERATED_KNOWLEDGE" not in prompt
    saved = summary.read_text()
    assert all(fact in saved for fact in facts)
    assert "[[concepts/missing]]" not in saved
    metadata = frontmatter.parse(saved)
    assert metadata["description"] == "操作要求"
    assert next(line for line in saved.splitlines() if line.startswith("# ")) == "# 操作要求"
    assert metadata["full_text"] == f"sources/handbook.{suffix}"
    assert metadata["custom_field"] == "retained"
    assert metadata["unit_kind"] == source_kind
    assert source.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["pages", "text"])
async def test_one_oversized_unit_reads_its_tail_with_native_continuation(
    kb_dir, monkeypatch, source_kind
):
    from openkb.agent.compiler import compile_long_doc

    wiki = kb_dir / "wiki"
    text = "前置条件 " + "连续的长段落记录；" * 5000 + "末尾例外：只有安全联锁启用才允许操作。"
    suffix = "json" if source_kind == "pages" else "md"
    (wiki / f"sources/manual.{suffix}").write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": text}], ensure_ascii=False)
        if source_kind == "pages"
        else text
    )
    summary = wiki / "summaries/manual.md"
    summary.write_text(f"---\nfull_text: sources/manual.{suffix}\n---\nNavigation.")
    ranges = []

    def response(**kwargs):
        if "Based on the summary above" in str(kwargs["messages"]):
            result = {}
        else:
            content = kwargs["messages"][1]["content"]
            batch = json.loads(content.split("\n", 1)[1])
            ranges.append(batch)
            result = {"description": "操作说明", "content": batch["content"]}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(summary_reply(kwargs["messages"], result))
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.completion", response)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    await compile_long_doc("manual", summary, "id", kb_dir, "test-model", navigation=[])
    assert len(ranges) > 1
    assert all(batch["supplementary_context"] for batch in ranges[1:])
    assert all(
        context["primary"] is False
        for batch in ranges[1:]
        for context in batch["supplementary_context"]
    )
    assert "末尾例外：只有安全联锁启用才允许操作。" in summary.read_text()
    spans = [span for batch in ranges for span in batch.get("page_spans", [])]
    if source_kind == "pages":
        assert spans[0] == {"page": 1, "start": 0, "end": spans[0]["end"]}
        assert spans[-1]["end"] == len(text)
        assert all(a["end"] == b["start"] for a, b in zip(spans, spans[1:]))
    else:
        spans = [span for batch in ranges for span in batch["source_spans"]]
        assert spans[0][0] == 0 and spans[-1][1] == len(text)
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


@pytest.mark.asyncio
async def test_summary_admission_rejects_all_batches_before_first_send(
    kb_dir, model_service, monkeypatch
):
    from test_llm_execution import executor_for

    from openkb.agent.compiler import compile_long_doc
    from openkb.llm_execution import ModelBudgetExceeded

    text = "Recorded operating conditions.\n\n" * 1200
    (kb_dir / "wiki/sources/large.md").write_text(text)
    summary = kb_dir / "wiki/summaries/large.md"
    summary.write_text("---\nfull_text: sources/large.md\n---\nPrevious successful knowledge.")
    before = summary.read_bytes()
    executor = executor_for(model_service, max_calls=2, retries=0)
    with executor.activate(), pytest.raises(ModelBudgetExceeded, match="compile"):
        await compile_long_doc("large", summary, "large-id", kb_dir, "test-model", navigation=[])
    assert executor.calls == 0
    assert model_service.requests == []
    assert summary.read_bytes() == before


@pytest.mark.asyncio
async def test_summary_capacity_measures_the_actual_full_request(kb_dir, monkeypatch):
    from openkb.agent.compiler import compile_long_doc
    from openkb.execution_capacity import capacity_policy
    from openkb.model_tokenizer import TokenMeasure

    config = {
        "model": "gpt-4o-mini",
        "model_capacity": {"max_input_tokens": 3500, "tokenizer_model": "gpt-4o-mini"},
    }
    import yaml

    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(config))
    text = "# 操作要求\n\n" + "只有检修完成且安全联锁启用后才允许开启设备。\n\n" * 700
    (kb_dir / "wiki/sources/manual.md").write_text(text)
    summary = kb_dir / "wiki/summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nNavigation.")
    requests = []

    def response(**kwargs):
        if "Based on the summary above" in str(kwargs["messages"]):
            result = {}
        else:
            requests.append(kwargs)
            result = {"description": "操作要求", "content": "# 操作要求\n\n检修与联锁条件。"}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(summary_reply(kwargs["messages"], result))
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.completion", response)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    await compile_long_doc("manual", summary, "id", kb_dir, config["model"], navigation=[])
    assert len(requests) > 1
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    assert all(
        measure.payload_tokens(
            {"messages": request["messages"], "response_format": request["response_format"]}
        )
        <= 3500
        for request in requests
    )


def test_failed_middle_summary_batch_keeps_the_published_knowledge_and_index(kb_dir, monkeypatch):
    import asyncio

    from kb_fixtures import seed_document_index

    from openkb.application.recompilation import recompile_document

    seed_document_index(kb_dir, "large-id", "large")
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"large-hash": {"doc_name": "large", "type": "long_pdf", "doc_id": "large-id"}})
    )
    source = kb_dir / "wiki/sources/large.json"
    source.write_text(
        json.dumps(
            [
                {"page": i, "unit_kind": "page", "content": "Detailed original conditions. " * 900}
                for i in range(1, 4)
            ]
        )
    )
    summary = kb_dir / "wiki/summaries/large.md"
    summary.write_text("---\nfull_text: sources/large.json\n---\nPrevious published knowledge.")
    before = summary.read_bytes()
    index = {
        str(p.relative_to(kb_dir)): p.read_bytes()
        for p in (kb_dir / ".openkb").rglob("*large-id*")
        if p.is_file()
    }
    sends = []

    async def unavailable_after_first(**kwargs):
        sends.append(kwargs["messages"])
        if len(sends) == 2:
            raise ConnectionError("Temporary provider failure")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            summary_reply(
                                kwargs["messages"],
                                {"description": "Conditions", "content": "# First batch knowledge"},
                            )
                        )
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("litellm.acompletion", unavailable_after_first)
    result = asyncio.run(recompile_document(kb_dir, "large-hash"))
    assert result.status == "failed" and result.error_type == "ConnectionError"
    assert len(sends) == 2
    assert summary.read_bytes() == before
    assert index and all((kb_dir / path).read_bytes() == data for path, data in index.items())
    assert not list((kb_dir / ".openkb/journal").glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged", ['{"text": 42}', "[]", "null", "42", '"text"'])
async def test_corrupt_rich_original_never_falls_back_to_navigation_or_markdown(
    kb_dir, monkeypatch, damaged
):
    from openkb.agent.compiler import compile_long_doc
    from openkb.compilation_report import CompilationIncomplete

    summary = kb_dir / "wiki/summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nPrevious knowledge.")
    before = summary.read_bytes()
    (kb_dir / "wiki/sources/manual.content.json").write_text(damaged)
    (kb_dir / "wiki/sources/manual.md").write_text("A plausible original cannot hide map damage.")

    async def forbidden(**kwargs):
        raise AssertionError("Damaged originals must fail before a model send")

    monkeypatch.setattr("litellm.acompletion", forbidden)
    with pytest.raises(CompilationIncomplete, match="document_summary_reader_invalid"):
        await compile_long_doc("manual", summary, "id", kb_dir, "test-model", navigation=[])
    assert summary.read_bytes() == before


@pytest.mark.asyncio
async def test_planner_knows_when_document_descriptions_are_a_partial_prefix(kb_dir, monkeypatch):
    from openkb.agent.compiler import compile_long_doc

    wiki = kb_dir / "wiki"
    text = "Operating conditions must be inspected before dispatch.\n\n" * 1200
    (wiki / "sources/manual.md").write_text(text)
    summary = wiki / "summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nNavigation.")
    requests, bodies = [], []

    def response(**kwargs):
        requests.append(kwargs["messages"])
        if "Based on the summary above" in str(kwargs["messages"]):
            result = {}
        else:
            bodies.append(f"# Batch {len(bodies) + 1}\n\nOriginal operating conditions retained.")
            result = {"description": "Inspection conditions. " * 400, "content": bodies[-1]}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(summary_reply(kwargs["messages"], result))
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.acompletion", asynchronous)
    monkeypatch.setattr("litellm.completion", response)
    await compile_long_doc("manual", summary, "id", kb_dir, "test-model", navigation=[])
    plan = next(messages for messages in requests if "Based on the summary above" in str(messages))
    assert "Descriptions are a partial prefix" in str(plan)
    assert all(body in summary.read_text() for body in bodies)


@pytest.mark.asyncio
async def test_published_summary_title_and_description_cover_every_original_batch(
    kb_dir, monkeypatch
):
    from openkb import frontmatter
    from openkb.agent.compiler import compile_long_doc

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.json"
    source.write_text(
        json.dumps(
            [
                {"page": page, "unit_kind": "page", "content": "Original instructions. " * 800}
                for page in (1, 2, 3)
            ]
        )
    )
    original_bytes = source.read_bytes()
    summary = wiki / "summaries/manual.md"
    summary.write_text(
        "---\ntype: Summary\nfull_text: sources/manual.json\n"
        "custom_field: retained\n---\nOld knowledge."
    )
    descriptions, bodies, requests = [], [], []

    def response(**kwargs):
        requests.append(kwargs["messages"])
        if "Based on the summary above" in str(kwargs["messages"]):
            result = {}
        else:
            batch = json.loads(kwargs["messages"][1]["content"].split("\n", 1)[1])
            pages = sorted({span["page"] for span in batch["page_spans"]})
            descriptions.append(
                f"This batch covers physical pages {pages}; other pages are outside this batch."
            )
            bodies.append(
                f"# Physical pages {pages}\n\nOriginal instructions retained for this batch."
            )
            result = {"description": descriptions[-1], "content": bodies[-1]}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(summary_reply(kwargs["messages"], result))
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.acompletion", asynchronous)
    monkeypatch.setattr("litellm.completion", response)
    await compile_long_doc("manual", summary, "id", kb_dir, "test-model", navigation=[])
    assert len(bodies) > 1
    saved = summary.read_text()
    metadata = frontmatter.parse(saved)
    assert all(description in metadata["description"] for description in descriptions)
    assert "Document knowledge covers all" not in metadata["description"]
    assert next(line for line in saved.splitlines() if line.startswith("# ")) == "# manual"
    for index, body in enumerate(bodies, 1):
        assert body in saved
    assert "Original-source batch" not in saved
    assert len(requests) == len(bodies) + 1  # Assembly does not make a recompression call.
    assert metadata["full_text"] == "sources/manual.json"
    assert metadata["custom_field"] == "retained"
    assert source.read_bytes() == original_bytes
