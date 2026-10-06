"""Reject unsupported knowledge bases before business writes or model calls."""

import json

import pytest

from openkb.application.knowledge_bases import initialize_kb, open_kb


def contents(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("marker", [None, {"format": "unknown", "version": 99}])
def test_old_and_unknown_knowledge_bases_are_rejected_unchanged(tmp_path, monkeypatch, marker):
    from openkb.application.execution import ExecutionContext
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.locks import kb_ingest_lock

    root = tmp_path / "old"
    (root / ".openkb").mkdir(parents=True)
    (root / "wiki").mkdir()
    (root / ".openkb/config.yaml").write_text("model: openai/gpt-4o\n")
    (root / "wiki/original.md").write_text("Do not alter this old knowledge")
    if marker:
        (root / ".openkb/format.json").write_text(json.dumps(marker))
    before = contents(root)
    monkeypatch.setattr("litellm.completion", lambda *a, **kw: pytest.fail("Model called"))
    from openkb.kb_admin import delete_kb

    for operation in [lambda: open_kb(root), lambda: get_kb_list(root), lambda: delete_kb(root)]:
        with pytest.raises(ValueError, match="Unsupported knowledge-base format"):
            operation()
        assert contents(root) == before
    with pytest.raises(ValueError, match="Unsupported knowledge-base format"):
        with kb_ingest_lock(root / ".openkb"), ExecutionContext().begin(root):
            pytest.fail("Entered unsupported knowledge base")
    assert contents(root) == before


def test_new_creation_has_supported_format(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "new"
    initialize_kb(root, seed_environment=False)
    assert json.loads((root / ".openkb/format.json").read_text()) == {
        "format": "openkb.context-kb",
        "version": 1,
    }
    assert open_kb(root) == root
