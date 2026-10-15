"""Invalid plan delivery cannot complete publication or reopen an exhausted repair."""

import asyncio
import json
from collections import Counter

import pytest
from test_compile_windows import _checkpoints, _plan

from ultrakb.agent.compile_delivery import publish_compile_results
from ultrakb.agent.compile_plan import CompilePlanError, parse_compile_plan, plan_repair_message
from ultrakb.agent.compiler import _ENTITY_TYPE_LIST, _sanitize_concept_name
from ultrakb.agent.page_response import PageResponseError
from ultrakb.compilation_report import collect_compile_report
from ultrakb.locks import atomic_write_text

pytest_plugins = ("test_workbook_import",)


def _options(wiki):
    return {
        "existing": {
            kind: {p.stem for p in (wiki / kind).glob("*.md")} for kind in ("concepts", "entities")
        },
        "entity_types": frozenset(_ENTITY_TYPE_LIST),
        "sanitize": _sanitize_concept_name,
    }


def _live_profile(kb_dir, monkeypatch, live_llm):
    live_llm.install(monkeypatch)
    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\n"
        "role_generation:\n  compile:\n    max_tokens: 512\n    reasoning_effort: none\n"
        "model_capacity:\n  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\n",
    )


def test_group_arrays_preserve_all_concepts_and_entities(kb_dir):
    wiki = kb_dir / "wiki"
    raw = {
        "concepts": [{"name": f"concept-{i}", "title": f"Concept {i}"} for i in range(3)],
        "entities": [
            {"name": f"entity-{i}", "title": f"Entity {i}", "type": "product"} for i in range(7)
        ],
    }
    with collect_compile_report() as report:
        plan = parse_compile_plan(json.dumps(raw), json.loads, **_options(wiki))
        publish_compile_results(
            wiki,
            "fixture",
            plan,
            summary="Source facts.",
            concept_results=[
                (item["name"], f"# {item['title']}\n\nSource facts.", False, "Fixture")
                for item in plan.concepts.create
            ],
            entity_results=[
                (item["name"], f"# {item['title']}\n\nSource facts.", "Fixture", item["type"])
                for item in plan.entities.create
            ],
        )
    assert len(list((wiki / "concepts").glob("*.md"))) == 3
    assert len(list((wiki / "entities").glob("*.md"))) == 7
    assert report.unfinished == []
    assert "compile_plan_normalized" in report.quality


@pytest.mark.parametrize("bad_plan", [{"concepts": 42}, {"unknown": []}])
def test_failed_plan_is_persisted_and_public_retry_preserves_exhausted_repair(
    kb_dir, three_sheets, monkeypatch, live_llm, bad_plan
):
    from ultrakb.agent import compiler
    from ultrakb.agent.page_response import parse_page
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.application.documents import import_document
    from ultrakb.application.workbook_actions import retry_worksheet
    from ultrakb.compile_checkpoints import digest, document_checkpoints
    from ultrakb.knowledge_scope import resolve_scope
    from ultrakb.unit_publication import read_unit_publication

    _live_profile(kb_dir, monkeypatch, live_llm)
    deliveries = Counter()
    healthy_deliveries = {}

    async def compile_with_invalid_beta(doc_name, source_path, root, model, *args, **kwargs):
        if "BETA_SHEET" not in source_path.read_text():
            # This regression needs genuine healthy delivery, not redundant concept
            # creation. Run the production summary receiver and publisher separately.
            scope = resolve_scope(root, kwargs.get("scope"), writable=True)
            reader = CompileSourceWindows(
                scope.wiki_dir, doc_name, kb_dir=root, view_id=scope.view_id
            )
            source = source_path.read_text(encoding="utf-8")
            evidence = reader.current_original_evidence(source)
            messages = compiler.short_document_messages(
                doc_name, source, "", "en", original_evidence=evidence
            )
            messages.append(
                {
                    "role": "user",
                    "content": "Keep this worksheet summary concise. Preserve its sheet marker "
                    "and given values. Return exactly description/content strings; use original "
                    "excerpt references to transcribe source syntax instead of reconstructing it.",
                }
            )
            admitted = len(live_llm.calls)
            raw = await compiler._llm_call_page_async(
                model,
                messages,
                "worksheet-summary",
                bundle=kwargs.get("bundle"),
                response_protocol="summary-page-json",
                response_format={"type": "json_object"},
                max_tokens=384,
                original_excerpts=SourceExcerpts([evidence]),
                numeric_sources=[source],
            )
            delivered = parse_page(raw, strict=True)
            healthy_deliveries[doc_name] = (len(live_llm.calls) - admitted, delivered["content"])
            publish_compile_results(
                scope.wiki_dir,
                doc_name,
                parse_compile_plan("{}", json.loads, **_options(scope.wiki_dir)),
                summary=delivered["content"],
                doc_brief=delivered["description"],
                rewrite_summary=True,
            )
            return
        # The malformed payload is supplied directly to the production receiver.
        # This injects only a domain delivery failure; no model port returns data.
        scope = kwargs.get("scope")
        checkpoints = document_checkpoints(root, scope.view_id if scope else "legacy", doc_name)
        signature = digest(["malformed-plan-delivery-test-v1", source_path.read_text()])
        try:
            checkpoints.require_generation("plan", signature)
        except PageResponseError as error:
            raise CompilePlanError(str(error), error.code) from error
        failure = None
        assert checkpoints is not None
        for attempt in range(2):
            if attempt:
                checkpoints.claim_repair("plan", signature)
            deliveries[doc_name] += 1
            try:
                parse_compile_plan(
                    json.dumps(bad_plan), json.loads, **_options(source_path.parent.parent)
                )
            except CompilePlanError as error:
                failure = error
        assert failure is not None
        checkpoints.record_failure("plan", signature, failure.code)
        raise failure

    monkeypatch.setattr(compiler, "compile_short_doc", compile_with_invalid_beta)
    first = import_document(kb_dir, three_sheets)
    assert first.status == "partial", first
    units = {unit.name: unit for unit in first.units}
    assert {name: unit.status for name, unit in units.items()} == {
        "Alpha": "completed",
        "Beta": "failed",
        "Gamma": "completed",
    }
    for name in ("Alpha", "Gamma"):
        calls, body = healthy_deliveries[units[name].doc_name]
        assert 1 <= calls <= 2
        assert f"{name.upper()}_SHEET" in body
    failed = units["Beta"]
    assert failed.successful_revision_id is None and failed.knowledge_revision_id is None
    stored = read_unit_publication(kb_dir, failed.unit_id, failed.view_id)
    assert stored.quality and stored.unfinished == ("concepts", "entities")
    assert sum(deliveries.values()) == 2
    before = dict(deliveries)
    admitted = len(live_llm.calls)
    resumed = retry_worksheet(kb_dir, first.source_id, failed.unit_id)
    assert resumed.status == "failed", resumed
    assert dict(deliveries) == before
    assert len(live_llm.calls) == admitted
    assert resumed.units[0].successful_revision_id is None
    assert "compile_repair_exhausted" in str(resumed)
    for name in ("Alpha", "Gamma"):
        unit = units[name]
        assert (
            read_unit_publication(kb_dir, unit.unit_id, unit.view_id).knowledge_revision_id
            == unit.knowledge_revision_id
        )


def test_plan_repair_is_bounded_and_does_not_leave_unfinished(kb_dir):
    checkpoints = _checkpoints(kb_dir)
    options = _options(kb_dir / "wiki")
    raw = json.dumps({"concepts": 42})
    with collect_compile_report() as report:
        with pytest.raises(CompilePlanError, match="concepts") as failure:
            parse_compile_plan(raw, json.loads, **options)
        assert "corrected JSON plan" in plan_repair_message(failure.value, raw)
        checkpoints.claim_repair("plan", "same-request")
        parse_compile_plan('{"concepts":{},"entities":{}}', json.loads, repaired=True, **options)
        with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
            checkpoints.claim_repair("plan", "same-request")
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
def test_conflicting_plans_exhaust_once_and_preserve_existing_pages(kb_dir, plan):
    original = kb_dir / "wiki/concepts/existing.md"
    atomic_write_text(original, "Existing knowledge.")
    checkpoints = _checkpoints(kb_dir)
    for attempt in range(2):
        if attempt:
            checkpoints.claim_repair("plan", "same-request")
        with pytest.raises(CompilePlanError):
            parse_compile_plan(json.dumps(plan), json.loads, **_options(kb_dir / "wiki"))
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        checkpoints.claim_repair("plan", "same-request")
    assert original.read_text() == "Existing knowledge."
    assert list((kb_dir / "wiki/concepts").glob("*.md")) == [original]
    assert not list((kb_dir / "wiki/entities").glob("*.md"))


def test_incomplete_recompile_preserves_last_success(kb_dir, three_sheets, monkeypatch, live_llm):
    from office_publication_fixtures import publish_workbook

    from ultrakb.application.recompilation import recompile_document, select_recompilation
    from ultrakb.compilation_report import report_compile_issue
    from ultrakb.unit_publication import read_head, read_unit_publication

    _live_profile(kb_dir, monkeypatch, live_llm)
    admitted = len(live_llm.calls)
    first = publish_workbook(kb_dir, three_sheets, bundle=live_llm.credentials)
    assert first.status == "added", first
    assert {unit.name: unit.status for unit in first.units} == {
        "Alpha": "completed",
        "Beta": "completed",
        "Gamma": "completed",
    }
    assert len({unit.successful_revision_id for unit in first.units}) == 3
    assert all(
        unit.successful_source_revision_id == first.source_revision_id for unit in first.units
    )
    assert len(live_llm.calls) == admitted
    unit = first.units[0]
    head = read_head(kb_dir, unit.view_id)

    async def incomplete(*args, **kwargs):
        report_compile_issue("concept_generation_incomplete", "concepts")

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", incomplete)
    selected = select_recompilation(
        kb_dir, first.source_id, confirmation=True, unit_id=unit.unit_id
    )
    result = asyncio.run(
        recompile_document(kb_dir, first.source_id, unit_id=unit.unit_id, version=selected.version)
    )
    assert result.status == "failed" and "concepts" in result.unfinished
    assert read_head(kb_dir, unit.view_id) == head
    failed = read_unit_publication(kb_dir, unit.unit_id, unit.view_id)
    assert failed.knowledge_revision_id == unit.knowledge_revision_id
    assert failed.successful_revision_id == unit.successful_revision_id
    assert len(live_llm.calls) == admitted


def test_incomplete_import_guard_does_not_retry_or_publish(kb_dir, three_sheets, monkeypatch):
    from ultrakb.application.documents import import_document
    from ultrakb.compilation_report import report_compile_issue

    calls = []

    async def incomplete(*args, **kwargs):
        calls.append(1)
        report_compile_issue("entity_generation_incomplete", "entities")

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", incomplete)
    result = import_document(kb_dir, three_sheets)
    assert result.status == "failed"
    assert len(calls) == 3
    assert all(unit.knowledge_revision_id is None for unit in result.units)


def test_actual_page_failure_cannot_publish_other_pages(kb_dir, monkeypatch):
    from ultrakb.compilation_report import CompilationIncomplete
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.mutation import mutation_scope

    wiki = kb_dir / "wiki"
    plan = _plan(
        wiki,
        {
            "concepts": [{"name": "mechanism", "title": "Mechanism"}],
            "entities": [{"name": "product", "title": "Product", "type": "product"}],
        },
    )
    with (
        kb_ingest_lock(kb_dir / ".ultrakb"),
        collect_compile_report() as report,
        pytest.raises(CompilationIncomplete),
    ):
        with mutation_scope(kb_dir, [wiki], operation="failed-page-delivery"):
            publish_compile_results(
                wiki,
                "required",
                plan,
                summary="Required facts.",
                rewrite_summary=True,
                concept_results=[("mechanism", "# Mechanism\n\nFacts.", False, "Concept")],
                entity_results=[ValueError("Incomplete required entity page")],
            )
            assert (wiki / "concepts/mechanism.md").exists()
            report.require_complete()
    assert report.unfinished == ["entities"]
    assert "entity_generation_incomplete" in report.quality
    assert not list((wiki / "summaries").glob("*.md"))
    assert not list((wiki / "concepts").glob("*.md"))
    assert not list((wiki / "entities").glob("*.md"))
