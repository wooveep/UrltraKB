"""Overflow planning covers the whole retained catalog at the model boundary."""

import json

import pytest

from ultrakb.agent import compiler, document_summary
from ultrakb.agent.compile_capacity import CompileCapacity
from ultrakb.config import LlmCredentialBundle
from ultrakb.execution_capacity import capacity_policy, require_request_capacity
from ultrakb.llm_execution import CompletionExecutor, RoleBindings
from ultrakb.locks import atomic_write_text
from ultrakb.model_tokenizer import ContextCapacityError, TokenMeasure


@pytest.mark.asyncio
@pytest.mark.integration
async def test_oversized_navigation_completes_every_native_id(kb_dir, monkeypatch):
    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.json"
    atomic_write_text(source, json.dumps([{"page": 1, "content": "Original procedure."}]))
    original = source.read_bytes()
    summary = wiki / "summaries/manual.md"
    atomic_write_text(summary, "---\nfull_text: sources/manual.json\n---\nRetained summary.\n")
    children = [
        {
            "node_id": f"native-{i}",
            "title": f"Section {i}: " + "native title " * 30,
            "start_index": 1,
            "end_index": 1,
        }
        for i in range(120)
    ]
    navigation = [
        {"node_id": "root", "title": "Manual", "start_index": 1, "end_index": 1, "nodes": children}
    ]
    config = {
        "model": "deepseek/deepseek-flash",
        "language": "en",
        "model_capacity": {"max_input_bytes": 24000},
    }
    executor = CompletionExecutor(RoleBindings(config["model"], LlmCredentialBundle()))
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    sizing = CompileCapacity(executor, measure)
    covered = []

    async def retained_summary(*args, **kwargs):
        return document_summary.SummaryDraft("# Retained summary\n", "Overview.", "Overview.", ())

    def receive(model, messages, stage, **kwargs):
        require_request_capacity(measure, sizing.payload(messages), operation="compile")
        assert stage == "concepts-plan-partition"
        task = json.loads(messages[-1]["content"].split("\n", 1)[1])
        ids = [item["node_id"] for item in task["work"]]
        covered.extend(ids)
        return json.dumps(
            {
                "coverage": [
                    {"id": identity, "candidates": [], "reason": "Navigation only."}
                    for identity in ids
                ],
                "candidates": [],
            }
        )

    monkeypatch.setattr(document_summary, "build_document_summary", retained_summary)
    monkeypatch.setattr(compiler, "_llm_call", receive)
    monkeypatch.setattr("ultrakb.config.resolve_effective_config", lambda *args: (config, {}))
    with executor.activate():
        await compiler.compile_long_doc(
            "manual", summary, "document-id", kb_dir, config["model"], navigation=navigation
        )
    assert covered == ["root", *[f"native-{i}" for i in range(120)]]
    assert source.read_bytes() == original
    assert "Retained summary" in summary.read_text()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_oversized_existing_catalog_scans_the_tail_before_creating(kb_dir, monkeypatch):
    wiki = kb_dir / "wiki"
    atomic_write_text(
        wiki / "sources/manual.json", json.dumps([{"page": 1, "content": "Original procedure."}])
    )
    summary = wiki / "summaries/manual.md"
    atomic_write_text(summary, "---\nfull_text: sources/manual.json\n---\nRetained summary.\n")
    for index in range(80):
        atomic_write_text(
            wiki / "concepts" / f"existing-{index:03}.md",
            f"---\ntitle: Existing {index}\ndescription: "
            + "Known meaning. " * 30
            + "\n---\nBody.\n",
        )
    config = {
        "model": "deepseek/deepseek-flash",
        "language": "en",
        "model_capacity": {"max_input_bytes": 24000},
    }
    executor = CompletionExecutor(RoleBindings(config["model"], LlmCredentialBundle()))
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    sizing = CompileCapacity(executor, measure)
    examined = []

    async def retained_summary(*args, **kwargs):
        return document_summary.SummaryDraft("# Retained summary\n", "Overview.", "Overview.", ())

    def receive(model, messages, stage, **kwargs):
        require_request_capacity(measure, sizing.payload(messages), operation="compile")
        task = json.loads(messages[-1]["content"].split("\n", 1)[1])
        if stage == "concepts-plan-partition":
            return json.dumps(
                {
                    "coverage": [{"id": "native", "candidates": ["alias"], "reason": "Topic."}],
                    "candidates": [
                        {
                            "id": "alias",
                            "kind": "concepts",
                            "name": "new-alias",
                            "title": "Synonymous title",
                            "brief": "The same existing meaning under a different name.",
                        }
                    ],
                }
            )
        assert stage == "concepts-plan-identity"
        examined.extend(item["id"] for item in task["catalog"])
        return json.dumps(
            {
                "matches": [
                    {
                        "id": item["id"],
                        "relation": "same" if item["name"] == "existing-079" else "different",
                        "action": "related",
                        "reason": "Identity comparison.",
                    }
                    for item in task["catalog"]
                ]
            }
        )

    monkeypatch.setattr(document_summary, "build_document_summary", retained_summary)
    monkeypatch.setattr(compiler, "_llm_call", receive)
    monkeypatch.setattr("ultrakb.config.resolve_effective_config", lambda *args: (config, {}))
    with executor.activate():
        await compiler.compile_long_doc(
            "manual",
            summary,
            "document-id",
            kb_dir,
            config["model"],
            navigation=[{"node_id": "native", "title": "Topic", "start_index": 1, "end_index": 1}],
        )
    assert set(examined) == {f"concepts/existing-{i:03}" for i in range(80)}
    assert len(list((wiki / "concepts").glob("*.md"))) == 80
    assert "[[concepts/existing-079" in summary.read_text()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_short_document_later_plan_overflow_keeps_accepted_summary(kb_dir, monkeypatch):
    source = kb_dir / "wiki/sources/short.md"
    atomic_write_text(source, "The original explains a single necessary topic.\n")
    original = source.read_bytes()
    config = {
        "model": "deepseek/deepseek-flash",
        "language": "en",
        "model_capacity": {"max_input_bytes": 24000},
    }
    executor = CompletionExecutor(RoleBindings(config["model"], LlmCredentialBundle()))
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    sizing = CompileCapacity(executor, measure)
    stages = []

    def receive(model, messages, stage, **kwargs):
        require_request_capacity(measure, sizing.payload(messages), operation="compile")
        stages.append(stage)
        if stage == "summary":
            return json.dumps({"description": "Overview.", "content": "Accepted summary. " * 2000})
        assert stage == "concepts-plan-partition"
        task = json.loads(messages[-1]["content"].split("\n", 1)[1])
        assert "single necessary topic" in str(task)
        return json.dumps(
            {
                "coverage": [
                    {"id": item["node_id"], "candidates": [], "reason": "Covered by summary."}
                    for item in task["work"]
                ],
                "candidates": [],
            }
        )

    monkeypatch.setattr(compiler, "_llm_call", receive)
    monkeypatch.setattr("ultrakb.config.resolve_effective_config", lambda *args: (config, {}))
    with executor.activate():
        await compiler.compile_short_doc("short", source, kb_dir, config["model"])
    assert stages == ["summary", "concepts-plan-partition"]
    assert source.read_bytes() == original
    assert "Accepted summary." in (kb_dir / "wiki/summaries/short.md").read_text()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_known_irreducible_rules_fail_before_paying_for_summary(kb_dir, monkeypatch):
    source = kb_dir / "wiki/sources/short.md"
    atomic_write_text(source, "Original text.\n")
    config = {
        "model": "deepseek/deepseek-flash",
        "language": "en",
        "model_capacity": {"max_input_bytes": 24000},
    }
    executor = CompletionExecutor(RoleBindings(config["model"], LlmCredentialBundle()))
    CompileCapacity(executor, TokenMeasure(capacity_policy(config, custom_endpoint=False)))
    monkeypatch.setattr(compiler, "get_agents_md", lambda wiki: "Required rule. " * 4000)
    monkeypatch.setattr("ultrakb.config.resolve_effective_config", lambda *args: (config, {}))

    def forbidden(*args, **kwargs):
        pytest.fail("Known irreducible fixed rules must fail before a model request")

    monkeypatch.setattr(compiler, "_llm_call", forbidden)
    with executor.activate(), pytest.raises(ContextCapacityError, match="fixed"):
        await compiler.compile_short_doc("short", source, kb_dir, config["model"])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_oversized_plan_correction_recovers_through_complete_native_work(kb_dir, monkeypatch):
    wiki = kb_dir / "wiki"
    atomic_write_text(wiki / "sources/manual.json", '[{"page":1,"content":"Original."}]')
    summary = wiki / "summaries/manual.md"
    atomic_write_text(summary, "---\nfull_text: sources/manual.json\n---\nRetained summary.\n")
    config = {
        "model": "deepseek/deepseek-flash",
        "language": "en",
        "model_capacity": {"max_input_bytes": 24000},
    }
    executor = CompletionExecutor(RoleBindings(config["model"], LlmCredentialBundle()))
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    sizing = CompileCapacity(executor, measure)
    stages = []

    async def retained_summary(*args, **kwargs):
        return document_summary.SummaryDraft("# Retained summary\n", "Overview.", "Overview.", ())

    def receive(model, messages, stage, **kwargs):
        require_request_capacity(measure, sizing.payload(messages), operation="compile")
        stages.append(stage)
        if stage == "concepts-plan":
            assert stages == ["concepts-plan"]
            return json.dumps({"concepts": 42, "diagnostic": "Full retained model output. " * 2000})
        task = json.loads(messages[-1]["content"].split("\n", 1)[1])
        return json.dumps(
            {
                "coverage": [
                    {"id": item["node_id"], "candidates": [], "reason": "Navigation only."}
                    for item in task["work"]
                ],
                "candidates": [],
            }
        )

    monkeypatch.setattr(document_summary, "build_document_summary", retained_summary)
    monkeypatch.setattr(compiler, "_llm_call", receive)
    monkeypatch.setattr("ultrakb.config.resolve_effective_config", lambda *args: (config, {}))
    with executor.activate():
        await compiler.compile_long_doc(
            "manual",
            summary,
            "document-id",
            kb_dir,
            config["model"],
            navigation=[{"node_id": "native", "title": "Topic", "start_index": 1, "end_index": 1}],
        )
    assert stages == ["concepts-plan", "concepts-plan-partition"]
