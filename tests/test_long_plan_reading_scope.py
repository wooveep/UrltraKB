"""Multi-section plans confirm native reading scope before drafting a page."""

import json
from types import SimpleNamespace

import pytest

from openkb.agent.compile_plan import CompilePlanError
from openkb.agent.compiler import _compile_concepts
from openkb.agent.source_windows import CompileSourceWindows
from openkb.compilation_report import collect_compile_report


@pytest.fixture
def reading_scope(kb_dir):
    wiki = kb_dir / "wiki"
    pages = [
        "Unrelated preface.",
        "Irrigation procedure: begin the irrigation setup.",
        "Before continuing, isolate the supply and obtain a supervisor's approval.",
        "Irrigation pressure adjustment.",
        "Irrigation valve calibration.",
        "Irrigation schedule configuration.",
        "Irrigation flow verification.",
        "Irrigation setup completion.",
        "Unrelated storage maintenance.",
    ]
    (wiki / "sources/guide.json").write_text(
        json.dumps(
            [{"page": i, "unit_kind": "page", "content": text} for i, text in enumerate(pages, 1)]
        )
    )
    navigation = [
        {"node_id": "preface", "title": "Preface", "start_index": 1, "end_index": 1},
        {
            "node_id": "procedure",
            "title": "Irrigation procedure",
            "start_index": 2,
            "end_index": 2,
            "nodes": [
                {"node_id": f"step-{i}", "title": text, "start_index": i, "end_index": i}
                for i, text in enumerate(pages, 1)
                if 3 <= i <= 8
            ],
        },
        {"node_id": "maintenance", "title": "Maintenance", "start_index": 9, "end_index": 9},
    ]
    return CompileSourceWindows(wiki, "guide", navigation=navigation, kb_dir=kb_dir)


async def compile_pages(kb_dir, windows):
    with collect_compile_report() as report:
        await _compile_concepts(
            kb_dir / "wiki",
            kb_dir,
            "test-model",
            {"role": "system", "content": "Compile from original text."},
            {"role": "user", "content": "Document navigation is available."},
            "Irrigation setup",
            "guide",
            1,
            doc_type="pageindex",
            source_windows=windows,
        )
        report.require_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["concepts", "entities"])
@pytest.mark.parametrize("action", ["create", "update"])
async def test_repaired_parent_scope_reaches_actual_page_generation(
    kb_dir, monkeypatch, reading_scope, kind, action
):
    from test_compile_windows import patch_page_response

    from openkb.locks import atomic_write_text

    plans, generation = [], []
    item = {"name": "irrigation", "title": "Irrigation procedure"}
    if kind == "entities":
        item["type"] = "other"
    if action == "update":
        atomic_write_text(
            kb_dir / f"wiki/{kind}/irrigation.md",
            "---\ndescription: Irrigation notes\ntype: other\n"
            "sources: []\n---\n# Irrigation\n\nInitial notes.\n",
        )

    def plan(model, messages, *args, **kwargs):
        plans.append(messages)
        proposal = dict(item)
        if len(plans) == 2:
            proposal["reading_locations"] = {"node_ids": ["procedure"]}
        return json.dumps({kind: {action: [proposal]}})

    async def generate(model, messages, *args, **kwargs):
        generation.append(messages)
        result = {"description": "Irrigation procedure", "content": "Check the supply."}
        if kind == "entities":
            result["type"] = "other"
        return patch_page_response(messages, json.dumps(result))

    monkeypatch.setattr("openkb.agent.compiler._llm_call", plan)
    monkeypatch.setattr("openkb.agent.compiler._llm_call_async", generate)
    await compile_pages(kb_dir, reading_scope)

    assert len(plans) == 2
    assert "reading_locations" in plans[-1][-1]["content"]
    assert len(generation) == 1
    sent = json.dumps(generation[0])
    assert "isolate the supply and obtain a supervisor's approval" in sent
    assert "Irrigation setup completion" in sent
    assert "Unrelated storage maintenance" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("locations", [None, {}])
async def test_missing_scope_exhausts_only_the_existing_plan_repair(
    kb_dir, monkeypatch, reading_scope, locations
):
    plans = []

    def plan(model, messages, *args, **kwargs):
        plans.append(messages)
        item = {"name": "irrigation", "title": "Irrigation procedure"}
        if locations is not None:
            item["reading_locations"] = locations
        return json.dumps({"concepts": {"create": [item]}})

    async def generate(*args, **kwargs):
        pytest.fail("An unresolved multi-section plan must not generate a page")

    monkeypatch.setattr("openkb.agent.compiler._llm_call", plan)
    monkeypatch.setattr("openkb.agent.compiler._llm_call_async", generate)
    with pytest.raises(CompilePlanError):
        await compile_pages(kb_dir, reading_scope)
    assert len(plans) == 2
    assert not (kb_dir / "wiki/concepts/irrigation.md").exists()


@pytest.mark.asyncio
async def test_real_long_document_planning_exposes_parent_scope(kb_dir, monkeypatch, reading_scope):
    from openkb.agent.compiler import compile_long_doc

    plans, generation = [], []

    async def summary(*args, **kwargs):
        return SimpleNamespace(
            markdown="# Guide\n\nIrrigation setup and maintenance.",
            description="Irrigation guide",
            planning_overview="Irrigation procedure",
        )

    def plan(model, messages, *args, **kwargs):
        plans.append(messages)
        navigation = json.loads(messages[1]["content"].split("\n", 1)[1])
        prerequisite = next(n for n in navigation if n["node_id"] == "step-3")
        parent_id = prerequisite["parent_node_id"]
        parent = next(n for n in navigation if n["node_id"] == parent_id)
        assert parent["start_index"] == parent["end_index"] == 2
        assert len({n["node_id"] for n in navigation}) == len(navigation)
        return json.dumps(
            {
                "concepts": {
                    "create": [
                        {"name": "irrigation", "reading_locations": {"node_ids": [parent_id]}}
                    ]
                }
            }
        )

    async def generate(model, messages, *args, **kwargs):
        generation.append(messages)
        return json.dumps({"description": "Irrigation procedure", "content": "Check the supply."})

    monkeypatch.setattr("openkb.agent.document_summary.build_document_summary", summary)
    monkeypatch.setattr("openkb.agent.compiler._llm_call", plan)
    monkeypatch.setattr("openkb.agent.compiler._llm_call_async", generate)
    with collect_compile_report() as report:
        await compile_long_doc(
            "guide",
            kb_dir / "wiki/summaries/guide.md",
            "guide-index",
            kb_dir,
            "test-model",
            navigation=[
                n
                for n in reading_scope.navigation
                if n["node_id"] in {"preface", "procedure", "maintenance"}
            ],
        )
        report.require_complete()
    assert len(plans) == len(generation) == 1
    sent = json.dumps(generation[0])
    assert "isolate the supply and obtain a supervisor's approval" in sent
    assert "Irrigation setup completion" in sent
    assert "Unrelated storage maintenance" not in sent
