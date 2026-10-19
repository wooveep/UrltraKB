"""Reject unsupported knowledge bases before business writes or model calls."""

import json

import pytest

from ultrakb.application.knowledge_bases import initialize_kb, open_kb


def contents(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("marker", [None, {"format": "unknown", "version": 99}])
def test_old_and_unknown_knowledge_bases_are_rejected_unchanged(tmp_path, monkeypatch, marker):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.knowledge_bases import get_kb_list
    from ultrakb.locks import kb_ingest_lock

    root = tmp_path / "old"
    (root / ".ultrakb").mkdir(parents=True)
    (root / "wiki").mkdir()
    (root / ".ultrakb/config.yaml").write_text("model: openai/gpt-4o\n")
    (root / "wiki/original.md").write_text("Do not alter this old knowledge")
    if marker:
        (root / ".ultrakb/format.json").write_text(json.dumps(marker))
    before = contents(root)
    monkeypatch.setattr("litellm.completion", lambda *a, **kw: pytest.fail("Model called"))
    from ultrakb.kb_admin import delete_kb

    for operation in [lambda: open_kb(root), lambda: get_kb_list(root), lambda: delete_kb(root)]:
        with pytest.raises(ValueError):
            operation()
        assert contents(root) == before
    with pytest.raises(ValueError):
        with kb_ingest_lock(root / ".ultrakb"), ExecutionContext().begin(root):
            pytest.fail("Entered unsupported knowledge base")
    assert contents(root) == before


def test_new_creation_has_supported_format(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "new"
    initialize_kb(root, seed_environment=False)
    from ultrakb.storage.database import open_database

    with open_database(root, read_only=True) as db:
        assert db.connection.execute(
            "SELECT format, format_version FROM ukb_meta WHERE singleton = 1"
        ).fetchone() == ("ultrakb.context-kb", 2)
    assert not (root / ".ultrakb/format.json").exists()
    assert open_kb(root) == root
