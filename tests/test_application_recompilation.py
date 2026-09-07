"""Shared recompile outcomes and storage consistency at the model boundary."""

import asyncio
import json
from types import SimpleNamespace

import pytest


def test_failed_long_recompile_restores_previous_summary(kb_dir, monkeypatch):
    import litellm

    from openkb.application.recompilation import recompile_document

    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"long-hash": {"doc_name": "paper", "type": "long_pdf", "doc_id": "doc-1"}})
    )
    summary = kb_dir / "wiki/summaries/paper.md"
    original = "---\nsources: [raw/paper.pdf]\n---\n# Previous paper\n"
    summary.write_text(original)

    def unavailable(**kwargs):
        # Long compilation backfills metadata before contacting the model.
        assert "Summary" in summary.read_text()
        raise ConnectionError("private provider detail")

    monkeypatch.setattr(litellm, "completion", unavailable)
    result = asyncio.run(recompile_document(kb_dir, "long-hash"))
    assert result.status == "failed"
    assert result.error_type == "ConnectionError"
    assert result.unfinished == ("compilation",)
    assert not result.changes
    assert "private provider detail" not in repr(result)
    assert summary.read_text() == original
    assert not list((kb_dir / ".openkb/journal").glob("*.json"))


def test_recompile_selection_freezes_identity_but_loads_current_source(kb_dir, monkeypatch):
    import litellm

    from openkb.application.execution import ExecutionContext
    from openkb.application.recompilation import recompile_document, select_recompilation

    registry = kb_dir / ".openkb/hashes.json"
    registry.write_text(
        json.dumps({"first": {"name": "note.md", "doc_name": "note", "type": "md"}})
    )
    source = kb_dir / "wiki/sources/note.md"
    source.write_text("First version")
    selection = select_recompilation(kb_dir, all_docs=True)
    assert selection.status == "ready"
    source.write_text("Latest source")
    calls = []
    responses = iter(
        [{"description": "Note", "content": "# Compiled"}, {"create": [], "update": []}]
    )

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(responses))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

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
    assert len(calls) == 2


def test_native_recompile_obeys_captured_concurrency(kb_dir, monkeypatch):
    import litellm

    from openkb.application.execution import ExecutionContext
    from openkb.application.recompilation import recompile_document

    (kb_dir / ".openkb/config.yaml").write_text("model: openai/test\nconcurrency: 1\n")
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"h": {"doc_name": "note", "type": "md"}})
    )
    (kb_dir / "wiki/sources/note.md").write_text("Original note")

    def response(content):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    replies = iter(
        [
            {"description": "Note", "content": "# Note"},
            {"create": [{"name": name, "title": name} for name in ("one", "two", "three")]},
            "# Rewritten note",
        ]
    )
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response(next(replies)))
    active = peak = 0

    async def complete(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.03)
        active -= 1
        return response({"description": "Concept", "content": "# Concept"})

    monkeypatch.setattr(litellm, "acompletion", complete)
    result = asyncio.run(recompile_document(kb_dir, "h", context=ExecutionContext()))
    assert result.status == "compiled"
    assert peak == 1
    assert len(list((kb_dir / "wiki/concepts").glob("*.md"))) == 3


def test_invalid_registry_shape_is_rejected_before_selection(kb_dir):
    from openkb.application.recompilation import select_recompilation

    (kb_dir / ".openkb/hashes.json").write_text(json.dumps({"h": {"doc_name": ["bad"]}}))
    with pytest.raises(ValueError, match="registry"):
        select_recompilation(kb_dir, all_docs=True)


def test_confirmed_recompile_detects_later_page_edit_before_snapshot(kb_dir):
    from openkb.application.execution import ExecutionContext
    from openkb.application.recompilation import recompile_document, select_recompilation

    (kb_dir / ".openkb/hashes.json").write_text(json.dumps({"h": {"doc_name": "note"}}))
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
def test_degraded_compile_preserves_summary_and_reports_unfinished_stages(
    kb_dir, monkeypatch, plan, code
):
    import litellm

    from openkb.application.recompilation import recompile_document

    (kb_dir / ".openkb/hashes.json").write_text(json.dumps({"h": {"doc_name": "note"}}))
    (kb_dir / "wiki/sources/note.md").write_text("Source")
    responses = iter([json.dumps({"description": "Note", "content": "# Saved note"}), plan])
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(responses)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    result = asyncio.run(recompile_document(kb_dir, "h"))
    assert result.status == "compiled"
    assert code in result.quality
    assert result.unfinished == ("concepts", "entities")
    assert str(kb_dir / "wiki/summaries/note.md") in result.resources
    assert "# Saved note" in (kb_dir / "wiki/summaries/note.md").read_text()
