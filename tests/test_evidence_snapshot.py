"""Concurrent generation uses bounded, detached evidence without inheriting KB locks."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from openkb.evidence import BlockDraft, Evidence, ParseStore
from openkb.evidence_snapshot import EvidenceSnapshot
from openkb.locks import kb_ingest_lock
from openkb.sources import SourceStore
from tests.test_source_evidence import save_source


def prepared(kb_dir, tmp_path, *, context=""):
    file = tmp_path / "snapshot.txt"
    file.write_text("original")
    version = save_source(kb_dir, file)
    store = ParseStore(kb_dir)
    parsed = store.save(
        version,
        {"parser": "snapshot-test"},
        [
            BlockDraft(
                "abcdefghij",
                "paragraph",
                {"kind": "text", "line": 1, "headings": ["Setup"]},
                context=context,
            )
        ],
    )
    return store.reader(version, parsed), Evidence(
        version.source_id, version.id, parsed.id, parsed.blocks[0].id
    )


def test_snapshot_worker_reads_under_owner_lease_and_cannot_mutate_shared_metadata(
    kb_dir, tmp_path, monkeypatch
):
    reader, reference = prepared(kb_dir, tmp_path)
    with kb_ingest_lock(kb_dir / ".openkb"):
        snapshot = EvidenceSnapshot(reader)

        def forbidden(*args, **kwargs):
            pytest.fail("A worker accessed live KB files")

        monkeypatch.setattr(SourceStore, "asset", forbidden)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(snapshot.read, reference, max_chars=4) for _ in range(2)]
            chunks = [future.result(timeout=2) for future in futures]
        assert all(chunk.text == "abcd" and chunk.next_start == 4 for chunk in chunks)
        chunks[0].location["headings"].append("mutated")
        assert chunks[1].location["headings"] == ["Setup"]
        assert snapshot.read(replace(reference, start=4), max_chars=4).text == "efgh"
        assert snapshot.read(reference, max_chars=10).location["headings"] == ["Setup"]


def test_snapshot_rejects_cross_source_bounds_and_oversized_metadata(kb_dir, tmp_path):
    reader, reference = prepared(kb_dir, tmp_path, context="Header " * 1000)
    snapshot = EvidenceSnapshot(reader)
    for candidate, bound, message in (
        (reference, 0, "positive bound"),
        (reference, 4, "context exceeds"),
        (replace(reference, source_id="a" * 32), 8000, "source mismatch"),
        (replace(reference, version_id="a" * 64), 8000, "source mismatch"),
        (replace(reference, parse_id="a" * 64), 8000, "source mismatch"),
        (replace(reference, block_id="a" * 64), 8000, "block is missing"),
        (replace(reference, end=11), 8000, "span exceeds"),
    ):
        with pytest.raises(ValueError, match=message):
            snapshot.read(candidate, max_chars=bound)
    assert snapshot.read(reference, max_chars=8000).context == "Header " * 1000


def test_snapshot_checks_original_blob_integrity_before_concurrent_work(kb_dir, tmp_path):
    reader, _ = prepared(kb_dir, tmp_path)
    reader.sources.asset(reader.parsed.blocks[0].blob).write_text("corruption")
    with pytest.raises(ValueError, match="digest"):
        EvidenceSnapshot(reader)
