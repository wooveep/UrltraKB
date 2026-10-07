"""Malformed model plans cannot become completed publications or skipped retries."""

import asyncio
import json
from collections import Counter

import pytest

pytest_plugins = ("test_workbook_import",)


def model_boundary(monkeypatch, plan):
    calls = Counter()

    def response(model, messages, step, **kwargs):
        calls[step] += 1
        if step == "concepts-plan":
            return json.dumps(plan(messages))
        return json.dumps({"description": "Fixture", "content": "# Fixture\n\nSource facts."})

    async def page(*args, **kwargs):
        return response(*args, **kwargs)

    monkeypatch.setattr("openkb.agent.compiler._llm_call", response)
    monkeypatch.setattr("openkb.agent.compiler._llm_call_page_async", page)
    return calls


def test_group_arrays_preserve_all_concepts_and_entities(kb_dir, monkeypatch):
    from openkb.agent.compiler import _compile_concepts
    from openkb.compilation_report import collect_compile_report

    plan = {
        "concepts": [{"name": f"concept-{i}", "title": f"Concept {i}"} for i in range(3)],
        "entities": [
            {"name": f"entity-{i}", "title": f"Entity {i}", "type": "product"} for i in range(7)
        ],
    }
    calls = model_boundary(monkeypatch, lambda _: plan)
    with collect_compile_report() as report:
        asyncio.run(
            _compile_concepts(kb_dir / "wiki", kb_dir, "gpt-4o", {}, {}, "Summary", "fixture", 4)
        )
    assert len(list((kb_dir / "wiki/concepts").glob("*.md"))) == 3
    assert len(list((kb_dir / "wiki/entities").glob("*.md"))) == 7
    assert calls["concepts-plan"] == 1
    assert report.unfinished == []


@pytest.mark.parametrize("bad_plan", [{"concepts": 42}, {"unknown": []}])
def test_failed_plan_is_persisted_and_public_retry_runs(
    kb_dir, three_sheets, pdf_model, monkeypatch, bad_plan
):
    from openkb.application.documents import import_document
    from openkb.application.workbook_actions import retry_worksheet
    from openkb.unit_publication import read_unit_publication

    failing = True
    calls = model_boundary(
        monkeypatch,
        lambda messages: bad_plan
        if failing and "BETA_SHEET" in str(messages)
        else {"concepts": {}, "entities": {}},
    )
    first = import_document(kb_dir, three_sheets)
    assert first.status == "partial", first
    units = {unit.name: unit for unit in first.units}
    assert {name: unit.status for name, unit in units.items()} == {
        "Alpha": "completed",
        "Beta": "failed",
        "Gamma": "completed",
    }
    failed = units["Beta"]
    assert failed.successful_revision_id is None
    assert failed.knowledge_revision_id is None
    stored = read_unit_publication(kb_dir, failed.unit_id, failed.view_id)
    assert stored.quality and stored.unfinished == ("concepts", "entities")
    assert calls["concepts-plan"] == 4  # Each good sheet once, bad plan twice.
    failing = False
    before = calls.copy()
    resumed = retry_worksheet(kb_dir, first.source_id, failed.unit_id)
    assert resumed.status == "added", resumed
    assert not resumed.unfinished
    assert calls["concepts-plan"] == before["concepts-plan"] + 1
    assert resumed.units[0].successful_revision_id == failed.target_revision_id
    for name in ("Alpha", "Gamma"):
        unit = units[name]
        assert (
            read_unit_publication(kb_dir, unit.unit_id, unit.view_id).knowledge_revision_id
            == unit.knowledge_revision_id
        )


def test_plan_repair_is_bounded_and_does_not_leave_unfinished(kb_dir, monkeypatch):
    from openkb.agent.compiler import _compile_concepts
    from openkb.compilation_report import collect_compile_report

    plans = iter([{"concepts": 42}, {"concepts": {}, "entities": {}}])
    calls = model_boundary(monkeypatch, lambda _: next(plans))
    with collect_compile_report() as report:
        asyncio.run(
            _compile_concepts(kb_dir / "wiki", kb_dir, "gpt-4o", {}, {}, "Summary", "fixture", 1)
        )
    assert calls["concepts-plan"] == 2
    assert not report.unfinished
    assert "compile_plan_repaired" in report.quality


@pytest.mark.parametrize(
    "plan",
    [
        {"concepts": {"create": [{"name": "existing"}]}},
        {"concepts": {"update": [{"name": "missing"}]}},
        {"concepts": {"related": ["missing"]}},
        {"concepts": [{"name": "new"}, {"name": "new"}]},
        {"entities": [{"name": "new", "type": "unsupported"}]},
    ],
)
def test_conflicting_plans_exhaust_once_and_preserve_existing_pages(kb_dir, monkeypatch, plan):
    from openkb.agent.compile_plan import CompilePlanError
    from openkb.agent.compiler import _compile_concepts

    original = kb_dir / "wiki/concepts/existing.md"
    original.write_text("Existing knowledge.")
    calls = model_boundary(monkeypatch, lambda _: plan)
    with pytest.raises(CompilePlanError):
        asyncio.run(
            _compile_concepts(kb_dir / "wiki", kb_dir, "gpt-4o", {}, {}, "Summary", "fixture", 1)
        )
    assert calls["concepts-plan"] == 2
    assert original.read_text() == "Existing knowledge."
    assert list((kb_dir / "wiki/concepts").glob("*.md")) == [original]
    assert not list((kb_dir / "wiki/entities").glob("*.md"))


def test_incomplete_recompile_preserves_last_success(kb_dir, three_sheets, pdf_model, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document, select_recompilation
    from openkb.compilation_report import report_compile_issue
    from openkb.unit_publication import read_head, read_unit_publication

    model_boundary(monkeypatch, lambda _: {"concepts": {}, "entities": {}})
    first = import_document(kb_dir, three_sheets)
    unit = first.units[0]
    head = read_head(kb_dir, unit.view_id)

    async def incomplete(*args, **kwargs):
        report_compile_issue("concept_generation_incomplete", "concepts")

    monkeypatch.setattr("openkb.agent.compiler.compile_short_doc", incomplete)
    selected = select_recompilation(
        kb_dir, first.source_id, confirmation=True, unit_id=unit.unit_id
    )
    result = asyncio.run(
        recompile_document(kb_dir, first.source_id, unit_id=unit.unit_id, version=selected.version)
    )
    assert result.status == "failed"
    assert "concepts" in result.unfinished
    assert read_head(kb_dir, unit.view_id) == head
    failed = read_unit_publication(kb_dir, unit.unit_id, unit.view_id)
    assert failed.knowledge_revision_id == unit.knowledge_revision_id
    assert failed.successful_revision_id == unit.successful_revision_id


def test_incomplete_import_guard_does_not_retry_or_publish(
    kb_dir, three_sheets, pdf_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.compilation_report import report_compile_issue

    calls = []

    async def incomplete(*args, **kwargs):
        calls.append(1)
        report_compile_issue("entity_generation_incomplete", "entities")

    monkeypatch.setattr("openkb.agent.compiler.compile_short_doc", incomplete)
    result = import_document(kb_dir, three_sheets)
    assert result.status == "failed"
    assert len(calls) == 3
    assert all(unit.knowledge_revision_id is None for unit in result.units)
    assert "entities" in result.unfinished


def test_actual_page_failure_cannot_publish_other_pages(kb_dir, pdf_model, monkeypatch):
    from openpyxl import Workbook

    from openkb.application.documents import import_document
    from openkb.unit_publication import read_head

    path = kb_dir / "pages.xlsx"
    book = Workbook()
    book.active["A1"] = "Worksheet facts."
    book.save(path)
    calls = model_boundary(
        monkeypatch,
        lambda _: {
            "concepts": [{"name": "mechanism", "title": "Mechanism"}],
            "entities": [{"name": "product", "title": "Product", "type": "product"}],
        },
    )

    async def page(model, messages, step, **kwargs):
        if step.startswith("entity:"):
            raise ValueError("Incomplete required entity page")
        return json.dumps({"description": "Concept", "content": "# Mechanism\n\nFacts."})

    monkeypatch.setattr("openkb.agent.compiler._llm_call_page_async", page)
    result = import_document(kb_dir, path)
    assert result.status == "failed"
    assert result.unfinished == ("entities",)
    assert "entity_generation_incomplete" in result.quality
    unit = result.units[0]
    assert unit.knowledge_revision_id is None
    assert read_head(kb_dir, unit.view_id).knowledge_revision_id is None
    assert calls["concepts-plan"] == 1
