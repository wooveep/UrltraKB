"""Queued recompilation binds its selected registration without freezing the whole KB."""

import asyncio
import json

import pytest

from openkb.application.knowledge_bases import get_kb_list
from openkb.application.recompilation import recompile_document
from openkb.runtime.requests import RecompileDocument
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.fixture
def selection(kb_dir):
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"legacy": {"name": "note.md", "doc_name": "note", "type": "md"}})
    )
    (kb_dir / "wiki/sources/note.md").write_text("A source-backed requirement.")
    return get_kb_list(kb_dir)["documents"][0]


def test_unrelated_knowledge_changes_do_not_reject_a_queued_source(kb_dir, selection, monkeypatch):
    import litellm

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kw: response(evidence_response(json.loads(kw["messages"][-1]["content"]))),
    )
    registry = kb_dir / ".openkb/hashes.json"
    entries = json.loads(registry.read_text())
    entries["unrelated"] = {"name": "another.md", "doc_name": "another", "type": "md"}
    registry.write_text(json.dumps(entries))
    manual = kb_dir / "wiki/concepts/manual.md"
    manual.write_text("# Unrelated manual edit\nRetain this paragraph.")
    before = manual.read_bytes()
    result = asyncio.run(
        recompile_document(kb_dir, "legacy", source_revision=selection["recompile_revision"])
    )
    assert result.status == "unfinished" and result.message == "needs_acceptance"
    assert manual.read_bytes() == before


def test_changed_selected_registration_requires_reselection(kb_dir, selection, monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **kw: pytest.fail("Stale selection executed"))
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"legacy": {"name": "replacement.md", "doc_name": "replacement", "type": "md"}})
    )
    result = asyncio.run(
        recompile_document(kb_dir, "legacy", source_revision=selection["recompile_revision"])
    )
    assert result.status == "conflict"
    assert result.message == "Selected source changed; refresh and reselect it"


def test_request_requires_exactly_one_confirmation_identity():
    assert RecompileDocument("legacy", "whole-kb-version").source_revision is None
    assert RecompileDocument("legacy", None, source_revision="a" * 64).version is None
    for version, revision in [(None, None), ("version", "a" * 64), (None, "bad")]:
        with pytest.raises(ValueError):
            RecompileDocument("legacy", version, source_revision=revision)
