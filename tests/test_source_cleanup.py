"""Explicit history cleanup protects readable citations and shared content."""

import pytest

from openkb.application.documents import import_document
from openkb.application.source_actions import read_source_evidence
from openkb.evidence import Evidence, ParseStore
from openkb.sources import SourceStore


def test_cleanup_preserves_cited_versions_and_shared_assets(kb_dir, tmp_path, model_service):
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup

    original = tmp_path / "notes.md"
    original.write_text("First version: keep the cited timeout of 42 seconds.")
    first = import_document(kb_dir, original)
    parsed = ParseStore(kb_dir).load(first.parse_id)
    reference = Evidence(first.source_id, first.input_version, parsed.id, parsed.blocks[0].id)
    original.write_text("Second version: separate shared input.")
    second = import_document(kb_dir, original)
    independent = tmp_path / "independent.md"
    independent.write_text(original.read_text())
    other = import_document(kb_dir, independent)
    original.write_text("Third version: current input.")
    current = import_document(kb_dir, original)
    (kb_dir / "wiki/concepts/citation.md").write_text(
        f"Retained evidence: {reference.version_id} / {reference.parse_id} / {reference.block_id}"
    )
    preview = preview_history_cleanup(kb_dir)
    assert second.input_version in preview.versions
    assert first.input_version not in preview.versions
    assert current.input_version not in preview.versions
    assert other.input_version not in preview.versions
    cleanup_history(kb_dir, preview.id)
    assert "42 seconds" in read_source_evidence(kb_dir, reference, max_chars=100).text
    store = SourceStore(kb_dir)
    assert store.original(store.version(other.input_version)).read_text() == independent.read_text()
    with pytest.raises(FileNotFoundError):
        store.version(second.input_version)
    assert not preview_history_cleanup(kb_dir).files


def test_cleanup_refuses_a_preview_after_new_citations(kb_dir, tmp_path, model_service):
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup

    original = tmp_path / "notes.md"
    original.write_text("Old unreferenced material.")
    first = import_document(kb_dir, original)
    original.write_text("New retained material.")
    import_document(kb_dir, original)
    preview = preview_history_cleanup(kb_dir)
    (kb_dir / "wiki/concepts/citation.md").write_text(first.input_version)
    with pytest.raises(ValueError, match="changed"):
        cleanup_history(kb_dir, preview.id)
    assert SourceStore(kb_dir).version(first.input_version)
