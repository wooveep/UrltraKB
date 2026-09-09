"""Version-bound evidence remains readable when sources and parsing change."""

from dataclasses import replace

import pytest

from openkb.evidence import BlockDraft, Evidence, ParseStore
from openkb.inputs import prepared_input
from openkb.sources import SourceStore


def save_source(kb, file):
    with prepared_input(file) as ready:
        return SourceStore(kb).intake(ready)


def test_bounded_evidence_preserves_source_parse_and_original_location(kb_dir, tmp_path):
    file = tmp_path / "manual.md"
    file.write_text("Original")
    version = save_source(kb_dir, file)
    store = ParseStore(kb_dir)
    parsed = store.save(
        version,
        {"parser": "test-v1"},
        [
            BlockDraft(
                "abcdefghij", "paragraph", {"kind": "docx", "paragraph": 3, "headings": ["Setup"]}
            )
        ],
    )
    evidence = Evidence(version.source_id, version.id, parsed.id, parsed.blocks[0].id)
    chunk = store.read(evidence, max_chars=4)
    assert chunk.text == "abcd" and chunk.next_start == 4
    assert chunk.location == {"kind": "docx", "paragraph": 3, "headings": ["Setup"]}
    assert "page" not in chunk.location
    assert store.read(replace(evidence, start=chunk.next_start), max_chars=4).text == "efgh"
    file.write_text("Updated")
    changed = save_source(kb_dir, file)
    assert store.read(evidence, max_chars=10).text == "abcdefghij"
    with pytest.raises(ValueError, match="input"):
        store.read(replace(evidence, version_id=changed.id), max_chars=10)


def test_parse_reuse_shares_outputs_without_merging_provenance(kb_dir, tmp_path):
    first, second = tmp_path / "one.txt", tmp_path / "two.txt"
    first.write_text("same")
    second.write_bytes(first.read_bytes())
    one, two = save_source(kb_dir, first), save_source(kb_dir, second)
    store = ParseStore(kb_dir)
    profile = {"parser": "test-v1", "model": "none", "options": {"layout": True}}
    before = store.save(
        one, profile, [BlockDraft("body", "paragraph", {"kind": "text", "line": 1})]
    )
    assert store.find(two, profile) == before
    reparsed = store.save(
        one, profile, [BlockDraft("new body", "paragraph", {"kind": "text", "line": 1})]
    )
    assert before.id != reparsed.id
    reference = Evidence(two.source_id, two.id, before.id, before.blocks[0].id)
    assert store.read(reference, max_chars=10).text == "body"
    assert store.find(one, {**profile, "model": "different"}) is None


def test_page_confirmation_is_bound_to_source_and_parse_version(kb_dir, tmp_path):
    file = tmp_path / "manual.pdf"
    file.write_bytes(b"synthetic source for store contract")
    version = save_source(kb_dir, file)
    store = ParseStore(kb_dir)
    parsed = store.save(
        version,
        {"parser": "test-v1"},
        [],
        quality=[{"page": 2, "status": "needs_review", "reason": "blank_or_illustration"}],
    )
    assert not store.complete(version, parsed)
    store.confirm_page(version, parsed, 2, "legitimate_blank")
    assert store.complete(version, parsed)
    different = store.save(version, {"parser": "test-v2"}, [], quality=parsed.quality)
    assert not store.complete(version, different)


def test_corrupt_or_unbound_evidence_cannot_be_read(kb_dir, tmp_path):
    file = tmp_path / "manual.txt"
    file.write_text("body")
    version = save_source(kb_dir, file)
    store = ParseStore(kb_dir)
    parsed = store.save(
        version,
        {"parser": "test-v1"},
        [BlockDraft("text", "paragraph", {"kind": "text", "line": 1})],
    )
    evidence = Evidence(version.source_id, version.id, parsed.id, parsed.blocks[0].id)
    with pytest.raises(ValueError):
        store.read(replace(evidence, source_id="../outside"), max_chars=10)
    with pytest.raises(ValueError):
        store.read(evidence, max_chars=0)
    SourceStore(kb_dir).asset(parsed.blocks[0].blob).write_text("corruption")
    with pytest.raises(ValueError, match="digest"):
        store.read(evidence, max_chars=10)


def test_page_confirmation_cannot_bypass_a_missing_required_cloud_asset(kb_dir, tmp_path):
    from openkb.application.source_actions import confirm_source_page

    file = tmp_path / "manual.pdf"
    file.write_bytes(b"synthetic source for store contract")
    version = save_source(kb_dir, file)
    store = ParseStore(kb_dir)
    parsed = store.save(
        version,
        {"parser": "test-v1"},
        [],
        quality=[
            {"page": 1, "status": "needs_review", "reason": "cloud_required_asset_missing"},
        ],
    )
    store.select(version, parsed)
    with pytest.raises(ValueError, match="requires reprocessing"):
        confirm_source_page(
            kb_dir,
            version.source_id,
            version_id=version.id,
            parse_id=parsed.id,
            page=1,
            reason="legitimate_illustration",
        )
    assert not store.complete(version, parsed)


def test_bounded_evidence_does_not_return_an_unbounded_table_header(kb_dir, tmp_path):
    file = tmp_path / "manual.txt"
    file.write_text("Original table")
    version = save_source(kb_dir, file)
    store = ParseStore(kb_dir)
    parsed = store.save(
        version,
        {"parser": "test-v1"},
        [
            BlockDraft("body", "table", {"kind": "docx", "table": 1}, context="Header " * 1000),
        ],
    )
    reference = Evidence(version.source_id, version.id, parsed.id, parsed.blocks[0].id)
    with pytest.raises(ValueError, match="context exceeds"):
        store.read(reference, max_chars=4)
    assert store.read(reference, max_chars=8000).context == "Header " * 1000
