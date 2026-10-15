"""Legacy aggregate cleanup and Source withdrawal require distinct approval."""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from test_batch_removal import shared_sources

from ultrakb.application.batch_removal import execute_batch_removal, preview_batch_removal
from ultrakb.application.document_actions import DocumentIdentity
from ultrakb.file_state import file_versions
from ultrakb.locks import atomic_write_text, kb_ingest_lock
from ultrakb.source_catalog import read_source
from ultrakb.state import HashRegistry


def seed_legacy(kb, originals):
    identities = []
    with kb_ingest_lock(kb / ".ultrakb"):
        atomic_write_text(kb / "raw/shared-old.md", "Two legacy records share this local copy.")
        for name in ("old-a", "old-b"):
            external = originals / f"{name}.md"
            external.write_text("External original must stay.")
            identity = f"legacy-{name}"
            HashRegistry(kb / ".ultrakb/hashes.json").add(
                identity,
                {
                    "name": f"{name}.md",
                    "doc_name": name,
                    "type": "md",
                    "raw_path": "raw/shared-old.md",
                    "source_path": str(external),
                },
            )
            atomic_write_text(kb / f"wiki/summaries/{name}.md", f"# {name}\n\nOld review policy.")
            atomic_write_text(kb / f"wiki/sources/{name}.md", "Old retained body.")
            identities.append(DocumentIdentity("legacy", identity))
        atomic_write_text(
            kb / "wiki/concepts/legacy-shared.md",
            "---\nsources: [summaries/old-a.md, summaries/old-b.md]\n---\n"
            "# Shared legacy policy\n\nBoth records support this.",
        )
    return tuple(identities)


@pytest.fixture
def mixed(kb_dir, tmp_path):
    outside = tmp_path.parent / f"mixed-originals-{tmp_path.name}"
    outside.mkdir()
    legacy = seed_legacy(kb_dir, outside)
    sources = shared_sources(kb_dir, outside)
    return legacy + sources, outside


@pytest.mark.parametrize("keep_raw,keep_empty", [(False, False), (True, True)])
def test_aggregate_last_two_legacy_dependencies_and_new_history(
    kb_dir, mixed, keep_raw, keep_empty
):
    identities, outside = mixed
    review = preview_batch_removal(kb_dir, identities, keep_raw=keep_raw, keep_empty=keep_empty)
    assert "wiki/concepts/legacy-shared.md" in review.legacy_empty
    assert ("wiki/concepts/legacy-shared.md" in review.legacy_deleted) is not keep_empty
    assert ("raw/shared-old.md" in review.legacy_deleted) is not keep_raw
    # Neither individual legacy plan knows both owners will be withdrawn.
    for identity in identities[:2]:
        single = preview_batch_removal(kb_dir, (identity,))
        assert "wiki/concepts/legacy-shared.md" not in single.legacy_deleted
        assert "raw/shared-old.md" not in single.legacy_deleted
    before = file_versions(kb_dir, [kb_dir / ".ultrakb/knowledge/legacy/revisions"])
    result = execute_batch_removal(review, confirmed_groups=("source", "legacy"))
    assert result.status == "completed", result
    assert all(item.status == "completed" for item in result.items)
    assert not HashRegistry(kb_dir / ".ultrakb/hashes.json").all_entries()
    assert (kb_dir / "raw/shared-old.md").exists() is keep_raw
    assert (kb_dir / "wiki/concepts/legacy-shared.md").exists() is keep_empty
    assert before == file_versions(kb_dir, [kb_dir / ".ultrakb/knowledge/legacy/revisions"])
    assert all((outside / f"{name}.md").exists() for name in ("old-a", "old-b", "甲", "乙"))
    assert all(
        read_source(kb_dir, identity.value).removed
        for identity in identities
        if identity.kind == "source"
    )


def test_unconfirmed_group_executes_nothing(kb_dir, mixed):
    identities, _ = mixed
    result = execute_batch_removal(
        preview_batch_removal(kb_dir, identities), confirmed_groups=("source",)
    )
    assert result.status == "blocked" and all(item.status == "not_run" for item in result.items)
    assert len(HashRegistry(kb_dir / ".ultrakb/hashes.json").all_entries()) == 2
    assert not any(
        read_source(kb_dir, identity.value).removed
        for identity in identities
        if identity.kind == "source"
    )


def test_partial_legacy_transaction_records_facts_and_stops_other_group(kb_dir, mixed):
    identities, _ = mixed
    review = preview_batch_removal(kb_dir, identities)
    with patch.object(
        HashRegistry, "remove_by_hash", side_effect=OSError("Registry commit failed")
    ):
        result = execute_batch_removal(review, confirmed_groups=("source", "legacy"))
    assert result.status == "partial"
    assert (
        result.items[0].status == "partial"
        and result.items[0].changes
        and result.items[0].unfinished
    )
    assert all(item.status == "not_run" for item in result.items[1:])
    assert len(HashRegistry(kb_dir / ".ultrakb/hashes.json").all_entries()) == 2
    assert not any(
        read_source(kb_dir, identity.value).removed
        for identity in identities
        if identity.kind == "source"
    )


def test_options_and_shared_file_change_expire_confirmation(kb_dir, mixed):
    identities, _ = mixed
    initial = preview_batch_removal(kb_dir, identities)
    changed = preview_batch_removal(kb_dir, identities, keep_raw=True)
    assert changed != initial and "raw/shared-old.md" in changed.legacy_retained
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(kb_dir / "raw/shared-old.md", "Changed while confirming")
    result = execute_batch_removal(initial, confirmed_groups=("source", "legacy"))
    assert result.status == "blocked" and all(item.status == "not_run" for item in result.items)


def test_legacy_image_referenced_by_another_page_survives(kb_dir, tmp_path):
    outside = tmp_path / "external"
    outside.mkdir()
    identities = seed_legacy(kb_dir, outside)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        image = kb_dir / "wiki/sources/images/old-a/chart.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"image-content")
        atomic_write_text(
            kb_dir / "wiki/concepts/manual.md",
            "# Human note\n\n![chart](../sources/images/old-a/chart.png)",
        )
    review = preview_batch_removal(kb_dir, identities)
    assert "wiki/sources/images/old-a" in review.legacy_retained
    result = execute_batch_removal(review, confirmed_groups=("legacy",))
    assert result.status == "completed", result
    assert image.read_bytes() == b"image-content"


def test_legacy_identity_cannot_withdraw_source(kb_dir, mixed):
    identities, _ = mixed
    source = next(item for item in identities if item.kind == "source")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        HashRegistry(kb_dir / ".ultrakb/hashes.json").add(
            source.value, {"name": "collision.md", "doc_name": "collision", "type": "md"}
        )
    with pytest.raises(ValueError, match="身份"):
        preview_batch_removal(kb_dir, (source, DocumentIdentity("legacy", source.value)))


def test_native_mixed_batch_removal(tmp_path):
    pytest.importorskip("PySide6")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_batch_removal.py",
            "--headless",
            "--mixed",
            "--output",
            str(tmp_path / "gui"),
        ],
        env=dict(os.environ, PYTHONPATH=".", QT_QPA_PLATFORM="offscreen"),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "gui/result.json").read_text())
    assert len(report["checks"]) == 5 and report["model_sends"] == 0
