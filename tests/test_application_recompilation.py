"""Shared recompile outcomes and storage consistency at the model boundary."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from tests.http_model_fixture import evidence_response


def test_failed_long_recompile_restores_previous_summary(kb_dir, monkeypatch):
    import litellm

    from openkb.application.recompilation import recompile_document

    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"long-hash": {"doc_name": "paper", "type": "long_pdf", "doc_id": "doc-1"}})
    )
    summary = kb_dir / "wiki/summaries/paper.md"
    original = "---\nsources: [raw/paper.pdf]\n---\n# Previous paper\n"
    summary.write_text(original)

    (kb_dir / "wiki/sources/paper.json").write_text(
        json.dumps([{"page": 1, "content": "Original full text", "images": []}])
    )

    def unavailable(**kwargs):
        assert summary.read_text() == original
        raise ConnectionError("private provider detail")

    monkeypatch.setattr(litellm, "completion", unavailable)
    result = asyncio.run(recompile_document(kb_dir, "long-hash"))
    assert result.status == "failed"
    assert result.error_type == "ConnectionError"
    assert result.document.stage == "compiling"
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

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            evidence_response(json.loads(kwargs["messages"][-1]["content"]))
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr(litellm, "completion", completion)
    result = asyncio.run(recompile_document(kb_dir, selection.targets[0].file_hash))
    assert result.status == "unfinished" and result.message == "needs_acceptance"
    from openkb.application.source_actions import continue_source, review_source_proposal

    review = review_source_proposal(kb_dir, result.document.resume)
    completed = continue_source(
        kb_dir,
        result.document.source_id,
        version_id=result.document.input_version,
        proposal_id=result.document.resume,
        accept_pages=review["protected"],
    )
    assert completed.status == "added"
    assert "Latest source" in str(calls[0]["messages"])
    assert str(kb_dir / "wiki/summaries/note.md") in completed.resources
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
    assert len(calls) == 3


def test_native_recompile_obeys_captured_concurrency(kb_dir, monkeypatch):
    import litellm

    from openkb.application.execution import ExecutionContext
    from openkb.application.recompilation import recompile_document

    (kb_dir / ".openkb/config.yaml").write_text("model: openai/test\nconcurrency: 1\n")
    from processing_fixtures import configure_processing

    configure_processing(kb_dir)
    import yaml

    path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["processing"]["concurrency"] = 1
    path.write_text(yaml.safe_dump(settings))
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"h": {"doc_name": "note", "type": "md"}})
    )
    (kb_dir / "wiki/sources/note.md").write_text("Original note")

    def response(content):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    active = peak = 0

    def complete(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        time.sleep(0.03)
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            for item, unit in zip(value["units"], payload["units"]):
                item["facts"] = [
                    {"topic": name, "statement": unit["text"], "quote": unit["text"]}
                    for name in ("one", "two", "three")
                ]
        elif payload["stage"] == "planning":
            value = {
                "topics": [
                    {"name": name, "title": name, "kind": "concept", "members": [name]}
                    for name in payload["topics"]
                ]
            }
        active -= 1
        return response(value)

    monkeypatch.setattr(litellm, "completion", complete)
    result = asyncio.run(recompile_document(kb_dir, "h", context=ExecutionContext()))
    assert result.status == "unfinished" and result.message == "needs_acceptance"
    from openkb.application.source_actions import continue_source, review_source_proposal

    review = review_source_proposal(kb_dir, result.document.resume)
    completed = continue_source(
        kb_dir,
        result.document.source_id,
        version_id=result.document.input_version,
        proposal_id=result.document.resume,
        accept_pages=review["protected"],
    )
    assert completed.status == "added"
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
        ("not JSON", "topic_plan_invalid"),
        (json.dumps({"topics": "not-a-list"}), "topic_plan_invalid"),
        (json.dumps({"topics": [{"name": ["bad"]}]}), "topic_plan_invalid"),
    ],
)
def test_degraded_compile_preserves_summary_and_reports_unfinished_stages(
    kb_dir, monkeypatch, plan, code
):
    import litellm

    from openkb.application.recompilation import recompile_document

    (kb_dir / ".openkb/hashes.json").write_text(json.dumps({"h": {"doc_name": "note"}}))
    (kb_dir / "wiki/sources/note.md").write_text("Source")
    (kb_dir / "wiki/summaries/note.md").write_text("Previous summary")

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        content = json.dumps(evidence_response(payload)) if payload["stage"] == "facts" else plan
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr(litellm, "completion", completion)
    result = asyncio.run(recompile_document(kb_dir, "h"))
    assert result.status == "unfinished"
    assert result.message == code
    assert result.unfinished == ("planning",)
    assert (kb_dir / "wiki/summaries/note.md").read_text() == "Previous summary"
