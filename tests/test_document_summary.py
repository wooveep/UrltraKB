"""Long summaries retain document knowledge independently of topic selection."""

import json

import pytest


async def generate_summary(messages, stage, originals, sections, live_llm):
    from ultrakb.agent.compiler import _llm_call_page_async
    from ultrakb.agent.document_summary import PROMPT_VERSION
    from ultrakb.agent.page_response import parse_summary
    from ultrakb.agent.source_excerpts import SourceExcerpts

    raw = await _llm_call_page_async(
        "deepseek/deepseek-flash",
        messages,
        stage,
        bundle=live_llm.credentials,
        response_format={"type": "json_object"},
        max_tokens=512,
        numeric_sources=[item["content"] for item in originals],
        original_excerpts=SourceExcerpts(originals),
        prompt_version=PROMPT_VERSION,
        response_protocol="summary-json",
        expected_sections=sections,
    )
    return parse_summary(raw, sections)


async def real_summary(kb_dir, name, live_llm, monkeypatch, *, max_chars=None):
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, build_document_summary
    from ultrakb.agent.source_windows import CompileSourceWindows

    live_llm.install(monkeypatch)
    windows = CompileSourceWindows(kb_dir / "wiki", name, navigation=[])
    records, decoded = [], []

    async def generate(messages, stage, originals, sections):
        records.append(messages)
        result = await generate_summary(messages, stage, originals, sections, live_llm)
        decoded.append(result)
        return result

    def fits(messages):
        return (
            max_chars is None
            or len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"]) <= max_chars
        )

    draft = await build_document_summary(
        windows,
        system_message={"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
        generate=generate,
        fits=fits,
    )
    return draft, decoded, records


@pytest.mark.asyncio
async def test_planner_receives_all_native_nodes_without_cached_text(kb_dir):
    from ultrakb.source_navigation import planning_navigation

    nodes = [
        {
            "node_id": "first",
            "title": "First topic",
            "start_index": 1,
            "end_index": 1,
            "text": "CACHED_ORIGINAL_TEXT",
            "summary": "A generated label.",
        },
        {"node_id": "tail", "title": "Last topic", "start_index": 2, "end_index": 3},
    ]
    records = planning_navigation(nodes)
    assert [n["node_id"] for n in records] == ["first", "tail"]
    assert records[1]["end_index"] == 3
    assert "CACHED_ORIGINAL_TEXT" not in json.dumps(records)
    assert records[0]["navigation_summary"] == "A generated label."


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["pages", "text"])
async def test_empty_topic_plan_still_summarizes_every_original_unit(
    kb_dir, monkeypatch, source_kind, live_llm
):
    from ultrakb import frontmatter
    from ultrakb.agent.compiler import _write_summary
    from ultrakb.locks import atomic_write_text

    wiki = kb_dir / "wiki"
    facts = [
        "Orion costs 17 credits.",
        "Inspect the safety interlock before opening.",
        "Nadir costs 23 credits.",
    ]
    suffix = "json" if source_kind == "pages" else "md"
    source = wiki / f"sources/handbook.{suffix}"
    atomic_write_text(
        source,
        json.dumps(
            [{"page": i, "unit_kind": "page", "content": fact} for i, fact in enumerate(facts, 1)]
        )
        if source_kind == "pages"
        else "\n\n".join(facts),
    )
    before = source.read_bytes()
    summary = wiki / "summaries/handbook.md"
    atomic_write_text(
        summary,
        f"---\ntype: Summary\nunit_kind: {source_kind}\n"
        f"full_text: sources/handbook.{suffix}\ncustom_field: retained\n---\n"
        "STALE_GENERATED_KNOWLEDGE\n[[concepts/missing]]",
    )
    # Document coverage is independent of a topic plan and empty native navigation.
    draft, decoded, sends = await real_summary(kb_dir, "handbook", live_llm, monkeypatch)
    assert len(decoded) == 1
    assert all(fact in str(sends[0]) for fact in facts)
    assert "STALE_GENERATED_KNOWLEDGE" not in str(sends)
    _write_summary(
        wiki,
        "handbook",
        draft.markdown,
        doc_type="pageindex",
        description=draft.description,
        full_text=f"sources/handbook.{suffix}",
    )
    saved = summary.read_text()
    assert all(fact in saved.lower() for fact in ("orion", "17", "nadir", "23", "interlock"))
    assert "[[concepts/missing]]" not in saved
    metadata = frontmatter.parse(saved)
    assert metadata["description"] == decoded[0]["description"]
    assert saved.split("---", 2)[-1].strip() == draft.markdown.strip()
    assert metadata["full_text"] == f"sources/handbook.{suffix}"
    assert metadata["custom_field"] == "retained"
    assert metadata["unit_kind"] == source_kind
    assert source.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["pages", "text"])
async def test_one_oversized_unit_reads_its_tail_with_native_continuation(
    kb_dir, monkeypatch, source_kind, live_llm
):
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, summary_messages, window_sections
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.locks import atomic_write_text

    wiki = kb_dir / "wiki"
    text = (
        "Precondition: inspect equipment. "
        + "Keep the door closed. " * 12
        + ("Tail exception: operation is permitted only with the safety interlock enabled.")
    )
    suffix = "json" if source_kind == "pages" else "md"
    atomic_write_text(
        wiki / f"sources/manual.{suffix}",
        json.dumps([{"page": 1, "unit_kind": "page", "content": text}])
        if source_kind == "pages"
        else text,
    )
    atomic_write_text(
        wiki / "summaries/manual.md", f"---\nfull_text: sources/manual.{suffix}\n---\nNavigation."
    )
    windows = CompileSourceWindows(wiki, "manual", navigation=[])
    # Exercise project window/continuation logic locally, with a tiny original.
    ranges = list(windows.iter_document_windows(fits=lambda batch: len(batch["content"]) <= 140))
    assert len(ranges) > 1
    assert all(batch["supplementary_context"] for batch in ranges[1:])
    assert all(
        context["primary"] is False
        for batch in ranges[1:]
        for context in batch["supplementary_context"]
    )
    if source_kind == "pages":
        spans = [span for batch in ranges for span in batch["page_spans"]]
        assert spans[0]["page"] == 1 and spans[0]["start"] == 0
        assert spans[-1]["end"] == len(text)
        assert all(a["end"] == b["start"] for a, b in zip(spans, spans[1:]))
    else:
        spans = [span for batch in ranges for span in batch["source_spans"]]
        assert spans[0][0] == 0 and spans[-1][1] == len(text)
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    # Only the meaningful tail and its local prerequisite context need the model.
    live_llm.install(monkeypatch)
    batch = ranges[-1]
    messages = summary_messages(
        {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}, "manual", batch
    )
    decoded = await generate_summary(
        messages,
        "document-summary:tail",
        [batch, *batch["supplementary_context"]],
        tuple(section["id"] for section in window_sections(batch)),
        live_llm,
    )
    body = "\n".join(section["content"] for section in decoded["sections"])
    assert "interlock" in body.lower() and "enabled" in body.lower()


@pytest.mark.asyncio
async def test_summary_admission_rejects_all_batches_before_first_send(kb_dir):
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, build_document_summary
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        RoleBindings,
    )
    from ultrakb.locks import atomic_write_text

    atomic_write_text(kb_dir / "wiki/sources/large.md", "Original conditions.\n\n" * 6)
    summary = kb_dir / "wiki/summaries/large.md"
    atomic_write_text(
        summary, "---\nfull_text: sources/large.md\n---\nPrevious successful knowledge."
    )
    before = summary.read_bytes()
    windows = CompileSourceWindows(kb_dir / "wiki", "large", navigation=[])
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()),
        ModelCallPolicy(max_calls=2, retries=0),
    )

    async def forbidden(*args):
        raise AssertionError("All summary batches must be admitted before generation")

    def fits(messages):
        return len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"]) <= 30

    with executor.activate(), pytest.raises(ModelBudgetExceeded, match="compile"):
        await build_document_summary(
            windows,
            system_message={"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
            generate=forbidden,
            fits=fits,
        )
    assert executor.calls == 0
    assert summary.read_bytes() == before


def test_summary_capacity_measures_the_actual_full_request(kb_dir):
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, summary_capacity, summary_messages
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.locks import atomic_write_text
    from ultrakb.model_tokenizer import TokenMeasure

    config = {
        "model": "deepseek/deepseek-flash",
        "model_capacity": {"max_input_tokens": 1800, "tokenizer_model": "gpt-4o-mini"},
    }
    text = "# 操作要求\n\n" + "只有检修完成且安全联锁启用后才允许开启设备。\n\n" * 6
    source = kb_dir / "wiki/sources/manual.md"
    atomic_write_text(source, text)
    atomic_write_text(
        kb_dir / "wiki/summaries/manual.md", "---\nfull_text: sources/manual.md\n---\nNavigation."
    )
    windows = CompileSourceWindows(kb_dir / "wiki", "manual", navigation=[])
    system = {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}
    fits = summary_capacity(config, custom_endpoint=False)
    batches = list(
        windows.iter_document_windows(
            fits=lambda batch: fits(summary_messages(system, "manual", batch))
        )
    )
    requests = [
        {
            "messages": summary_messages(system, "manual", batch),
            "response_format": {"type": "json_object"},
        }
        for batch in batches
    ]
    assert requests
    assert "".join(batch["content"] for batch in batches) == text
    assert source.read_text() == text
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    assert all(measure.payload_tokens(request) <= measure.input_limit for request in requests)
    assert all(request["messages"][0] == system for request in requests)
    assert all(
        any(
            message["content"].startswith("Original excerpt catalog:\n")
            for message in request["messages"]
        )
        for request in requests
    )
    assert all(
        "检修" in str(request["messages"]) and "联锁" in str(request["messages"])
        for request in requests
    )
    full_tokens = measure.payload_tokens(requests[0])
    reduced = {
        **config,
        "model_capacity": {**config["model_capacity"], "max_input_tokens": full_tokens - 1},
    }
    assert not summary_capacity(reduced, custom_endpoint=False)(requests[0]["messages"])
    assert measure.payload_tokens({"messages": [requests[0]["messages"][1]]}) < full_tokens - 1


def test_failed_middle_summary_batch_keeps_the_published_knowledge_and_index(
    kb_dir, monkeypatch, live_llm
):
    import asyncio

    from kb_fixtures import seed_document_index

    from ultrakb.application.recompilation import recompile_document
    from ultrakb.locks import atomic_write_text

    seed_document_index(kb_dir, "large-id", "large")
    atomic_write_text(
        kb_dir / ".ultrakb/hashes.json",
        json.dumps({"large-hash": {"doc_name": "large", "type": "long_pdf", "doc_id": "large-id"}}),
    )
    atomic_write_text(
        kb_dir / "wiki/sources/large.json",
        json.dumps(
            [
                {
                    "page": page,
                    "unit_kind": "page",
                    "content": f"Unit {page}: inspect the safety interlock before opening.",
                }
                for page in range(1, 4)
            ]
        ),
    )
    summary = kb_dir / "wiki/summaries/large.md"
    atomic_write_text(
        summary, "---\nfull_text: sources/large.json\n---\nPrevious published knowledge."
    )
    before = summary.read_bytes()
    index = {
        str(p.relative_to(kb_dir)): p.read_bytes()
        for p in (kb_dir / ".ultrakb").rglob("*large-id*")
        if p.is_file()
    }
    live_llm.install(monkeypatch)
    sends = []

    async def interrupted(**kwargs):
        sends.append(kwargs["messages"])
        if len(sends) > 1:
            raise ConnectionError("Local interruption before the next provider send")
        return await live_llm.acompletion(**kwargs)

    monkeypatch.setattr("litellm.acompletion", interrupted)
    # Local grouping boundary, already verified independently; no model stress data.
    monkeypatch.setattr(
        "ultrakb.agent.document_summary.summary_capacity",
        lambda *a, **k: lambda messages: len(
            json.loads(messages[1]["content"].split("\n", 1)[1])["content"]
        )
        <= 80,
    )
    result = asyncio.run(recompile_document(kb_dir, "large-hash"))
    assert result.status == "failed" and result.error_type == "ConnectionError"
    assert len(sends) >= 2
    assert summary.read_bytes() == before
    assert index and all((kb_dir / path).read_bytes() == data for path, data in index.items())
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged", ['{"text": 42}', "[]", "null", "42", '"text"'])
@pytest.mark.integration
async def test_corrupt_rich_original_never_falls_back_to_navigation_or_markdown(
    kb_dir, monkeypatch, damaged
):
    from ultrakb.agent.compiler import compile_long_doc
    from ultrakb.compilation_report import CompilationIncomplete

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
async def test_planner_knows_when_document_descriptions_are_a_partial_prefix(kb_dir):
    from ultrakb.agent.document_summary import SummaryDraft, planning_overview

    bodies = [
        "# First\nOriginal operating conditions.",
        "# Last\nThe safety interlock is required.",
    ]
    descriptions = ["Inspection conditions. " * 3, "Safety conditions. " * 3]
    draft = SummaryDraft("\n\n".join(bodies), "\n".join(descriptions), "\n".join(descriptions), ())
    plan = planning_overview(draft, 50)
    assert "Descriptions are a partial prefix" in plan
    assert all(body in draft.markdown for body in bodies)
    assert plan[:50] == draft.planning_overview[:50]
    assert planning_overview(draft, 1000) == draft.planning_overview


@pytest.mark.asyncio
async def test_published_summary_title_and_description_cover_every_original_batch(
    kb_dir, monkeypatch, live_llm
):
    from ultrakb import frontmatter
    from ultrakb.agent.compiler import _write_summary
    from ultrakb.locks import atomic_write_text

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.json"
    atomic_write_text(
        source,
        json.dumps(
            [
                {
                    "page": page,
                    "unit_kind": "page",
                    "content": f"Physical page {page}: Orion requires {page} inspections.",
                }
                for page in (1, 2, 3)
            ]
        ),
    )
    original_bytes = source.read_bytes()
    summary = wiki / "summaries/manual.md"
    atomic_write_text(
        summary,
        "---\ntype: Summary\nfull_text: sources/manual.json\n"
        "custom_field: retained\n---\nOld knowledge.",
    )
    draft, decoded, requests = await real_summary(
        kb_dir, "manual", live_llm, monkeypatch, max_chars=60
    )
    descriptions = [item["description"] for item in decoded]
    bodies = ["\n\n".join(section["content"] for section in item["sections"]) for item in decoded]
    assert len(bodies) > 1
    _write_summary(
        wiki,
        "manual",
        draft.markdown,
        doc_type="pageindex",
        description=draft.description,
        full_text="sources/manual.json",
    )
    saved = summary.read_text()
    metadata = frontmatter.parse(saved)
    assert all(description in metadata["description"] for description in descriptions)
    assert "Document knowledge covers all" not in metadata["description"]
    assert next(line for line in saved.splitlines() if line.startswith("# ")) == "# manual"
    assert all(body in saved for body in bodies)
    assert "Original-source batch" not in saved
    assert len(requests) == len(bodies)  # Assembly makes no recompression call.
    assert metadata["full_text"] == "sources/manual.json"
    assert metadata["custom_field"] == "retained"
    assert source.read_bytes() == original_bytes
