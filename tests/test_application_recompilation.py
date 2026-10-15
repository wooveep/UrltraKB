"""Shared recompile outcomes and storage consistency at the model boundary."""

import asyncio
import json

import pytest
from application_llm_fixtures import real_application_model  # noqa: F401


def test_failed_long_recompile_restores_previous_summary(kb_dir, monkeypatch):
    from kb_fixtures import seed_document_index

    from ultrakb.application.recompilation import recompile_document

    seed_document_index(kb_dir, "doc-1", "paper")
    (kb_dir / ".ultrakb/hashes.json").write_text(
        json.dumps({"long-hash": {"doc_name": "paper", "type": "long_pdf", "doc_id": "doc-1"}})
    )
    summary = kb_dir / "wiki/summaries/paper.md"
    original = "---\nsources: [raw/paper.pdf]\n---\n# Previous paper\n"
    summary.write_text(original)
    (kb_dir / "wiki/sources/paper.json").write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": "Retained PDF content"}])
    )

    async def unavailable(*args, **kwargs):
        # Metadata backfilling stays in the candidate until publication succeeds.
        assert summary.read_text() == original
        raise ConnectionError("private provider detail")

    monkeypatch.setattr("ultrakb.agent.compiler.compile_long_doc", unavailable)
    result = asyncio.run(recompile_document(kb_dir, "long-hash"))
    assert result.status == "failed"
    assert result.error_type == "ConnectionError"
    assert result.unfinished == ("compilation",)
    assert not result.changes
    assert "private provider detail" not in repr(result)
    assert summary.read_text() == original
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))


def test_recompile_selection_freezes_identity_but_loads_current_source(kb_dir, monkeypatch):
    import litellm

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.recompilation import recompile_document, select_recompilation

    registry = kb_dir / ".ultrakb/hashes.json"
    registry.write_text(
        json.dumps({"first": {"name": "note.md", "doc_name": "note", "type": "md"}})
    )
    source = kb_dir / "wiki/sources/note.md"
    source.write_text("First version")
    selection = select_recompilation(kb_dir, all_docs=True)
    assert selection.status == "ready"
    source.write_text("Latest source")
    calls = []
    genuine_completion = litellm.completion

    def completion(**kwargs):
        calls.append(kwargs)
        return genuine_completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    result = asyncio.run(recompile_document(kb_dir, selection.targets[0].file_hash))
    assert result.status == "compiled"
    assert "Latest source" in str(calls[0]["messages"])
    assert str(kb_dir / "wiki/summaries/note.md") in result.resources
    assert "updated: wiki/summaries/note.md" in result.changes
    # Reusing a filename must not redirect an already queued operation.
    registry.write_text(
        json.dumps({"replacement": {"name": "note.md", "doc_name": "note", "type": "md"}})
    )
    context = ExecutionContext()
    skipped = asyncio.run(
        recompile_document(kb_dir, selection.targets[0].file_hash, context=context)
    )
    assert skipped.status == "skipped"
    assert context.snapshot is None
    assert len(calls) >= 2


def test_native_recompile_obeys_captured_concurrency(kb_dir, monkeypatch):
    import litellm

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.recompilation import recompile_document
    from ultrakb.locks import atomic_write_text

    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\nconcurrency: 1\n",
    )
    (kb_dir / ".ultrakb/hashes.json").write_text(
        json.dumps({"h": {"doc_name": "note", "type": "md"}})
    )
    atomic_write_text(
        kb_dir / "wiki/sources/note.md",
        "# Irrigation procedure\nIsolate the supply before calibration.\n"
        "Set irrigation pressure to 17 kPa.\nThe irrigation alarm code is 43.\n",
    )
    active = peak = completed = 0
    genuine_completion = litellm.acompletion

    async def complete(**kwargs):
        nonlocal active, peak, completed
        active += 1
        peak = max(active, peak)
        try:
            response = await genuine_completion(**kwargs)
            completed += 1
            return response
        finally:
            active -= 1

    monkeypatch.setattr(litellm, "acompletion", complete)
    context = ExecutionContext()
    result = asyncio.run(recompile_document(kb_dir, "h", context=context))
    assert result.status == "compiled", result.message
    assert context.snapshot.values()["effective"]["concurrency"] == 1
    assert completed >= 1 and peak == 1
    body = (kb_dir / "wiki/summaries/note.md").read_text().lower()
    assert "17" in body and "43" in body and "isolate" in body


def test_invalid_registry_shape_is_rejected_before_selection(kb_dir):
    from ultrakb.application.recompilation import select_recompilation

    (kb_dir / ".ultrakb/hashes.json").write_text(json.dumps({"h": {"doc_name": ["bad"]}}))
    with pytest.raises(ValueError, match="registry"):
        select_recompilation(kb_dir, all_docs=True)


def test_confirmed_recompile_detects_later_page_edit_before_snapshot(kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.recompilation import recompile_document, select_recompilation

    (kb_dir / ".ultrakb/hashes.json").write_text(json.dumps({"h": {"doc_name": "note"}}))
    (kb_dir / "wiki/sources/note.md").write_text("Source")
    selection = select_recompilation(kb_dir, all_docs=True, confirmation=True)
    concept = kb_dir / "wiki/concepts/manual.md"
    concept.write_text("Manual work after confirmation")
    context = ExecutionContext()
    result = asyncio.run(
        recompile_document(kb_dir, "h", context=context, version=selection.version)
    )
    assert result.status == "conflict"
    assert context.snapshot is None
    assert concept.read_text() == "Manual work after confirmation"


@pytest.mark.parametrize(
    "plan, code",
    [
        ("not JSON", "concept_plan_unparseable"),
        (json.dumps({"concepts": {"create": "not-a-list"}}), "malformed_plan_items"),
        (json.dumps({"entities": {"create": "not-a-list"}}), "malformed_plan_items"),
    ],
)
def test_degraded_compile_preserves_prior_summary_and_reports_failure(
    kb_dir, monkeypatch, plan, code
):
    from ultrakb.agent.compile_plan import CompilePlanError, normalize_plan
    from ultrakb.agent.compiler import _sanitize_concept_name
    from ultrakb.application.recompilation import recompile_document
    from ultrakb.compilation_report import report_compile_issue
    from ultrakb.locks import atomic_write_text

    (kb_dir / ".ultrakb/hashes.json").write_text(json.dumps({"h": {"doc_name": "note"}}))
    atomic_write_text(kb_dir / "wiki/sources/note.md", "Source")
    atomic_write_text(kb_dir / "wiki/summaries/note.md", "# Prior note")

    async def validate_invalid_plan(*args, **kwargs):
        # Exercise the parser with protocol data, then verify application rollback
        # and diagnostics at the compiler service boundary.
        with pytest.raises(ValueError):
            normalize_plan(
                json.loads(plan),
                existing={"concepts": set(), "entities": set()},
                entity_types=frozenset({"other"}),
                sanitize=_sanitize_concept_name,
            )
        report_compile_issue(code, "concepts", "entities")
        raise CompilePlanError("Invalid plan remains unresolved", code)

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", validate_invalid_plan)
    result = asyncio.run(recompile_document(kb_dir, "h"))
    assert result.status == "failed"
    assert code in result.quality
    assert result.unfinished == ("concepts", "entities")
    assert (kb_dir / "wiki/summaries/note.md").read_text() == "# Prior note"
