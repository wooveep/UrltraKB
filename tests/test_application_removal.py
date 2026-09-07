"""Removal plans describe a stable view and retain retryable partial results."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from openkb.application.removal import preview_removal, remove_document
from openkb.state import HashRegistry


def seed(kb: Path) -> None:
    (kb / ".openkb/hashes.json").write_text(
        json.dumps(
            {
                "hash-paper": {
                    "name": "paper.pdf",
                    "doc_name": "paper",
                    "type": "short",
                    "raw_path": "raw/paper.pdf",
                }
            }
        )
    )
    (kb / "raw/paper.pdf").write_bytes(b"original")
    (kb / "wiki/summaries/paper.md").write_text("# Paper\n")
    (kb / "wiki/sources/paper.md").write_text("source\n")


def test_changed_plan_requires_another_confirmation(kb_dir):
    seed(kb_dir)
    preview = preview_removal(kb_dir, "hash-paper")
    added = kb_dir / "wiki/concepts/new.md"
    added.write_text("---\nsources: [summaries/paper.md]\n---\nNew work\n")
    result = remove_document(kb_dir, "hash-paper", version=preview.version)
    assert result.status == "conflict"
    assert added.exists() and (kb_dir / "raw/paper.pdf").exists()
    latest = preview_removal(kb_dir, "hash-paper")
    assert latest.version != preview.version
    result = remove_document(kb_dir, "hash-paper", version=latest.version)
    assert result.status == "removed" and not added.exists()
    assert not (kb_dir / "raw/paper.pdf").exists()


def test_wiki_failure_rolls_back_before_registry_commit(kb_dir):
    seed(kb_dir)
    with patch("openkb.agent.compiler.remove_doc_from_index", side_effect=OSError("write failed")):
        with pytest.raises(OSError):
            remove_document(kb_dir, "hash-paper")
    assert (kb_dir / "wiki/summaries/paper.md").read_text() == "# Paper\n"
    assert (kb_dir / "wiki/sources/paper.md").exists()
    assert "hash-paper" in HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()


def seed_index(kb: Path) -> Path:
    from pageindex.storage.sqlite import SQLiteStorage

    seed(kb)
    registry = kb / ".openkb/hashes.json"
    value = json.loads(registry.read_text())
    value["hash-paper"].update(type="long_pdf", doc_id="pi-paper")
    registry.write_text(json.dumps(value))
    blob = kb / ".openkb/files/default/pi-paper.pdf"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"indexed original")
    storage = SQLiteStorage(str(kb / ".openkb/pageindex.db"))
    try:
        storage.get_or_create_collection("default")
        storage.save_document(
            "default",
            "pi-paper",
            {"doc_name": "paper", "doc_type": "pdf", "file_path": str(blob), "file_hash": "x"},
        )
    finally:
        storage.close()
    return blob


def test_local_index_cleanup_uses_no_model_and_changes_no_environment(kb_dir):
    blob = seed_index(kb_dir)
    before = dict(os.environ)
    with patch("pageindex.PageIndexClient", side_effect=AssertionError("No model needed")):
        result = remove_document(kb_dir, "hash-paper")
    assert result.status == "removed" and not blob.exists()
    assert dict(os.environ) == before
    with sqlite3.connect(kb_dir / ".openkb/pageindex.db") as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_registry_failure_restores_index_and_raw_for_manual_retry(kb_dir):
    blob = seed_index(kb_dir)
    with patch.object(HashRegistry, "remove_by_hash", side_effect=OSError("registry write failed")):
        result = remove_document(kb_dir, "hash-paper")
    assert result.status == "partial"
    assert result.unfinished and result.retained
    assert blob.read_bytes() == b"indexed original"
    assert (kb_dir / "raw/paper.pdf").exists()
    assert not (kb_dir / "wiki/summaries/paper.md").exists()
    with sqlite3.connect(kb_dir / ".openkb/pageindex.db") as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert remove_document(kb_dir, "hash-paper").status == "removed"


def test_registry_cannot_direct_deletion_outside_raw(kb_dir):
    seed(kb_dir)
    outside = kb_dir.parent / "unrelated.txt"
    outside.write_text("keep")
    registry = kb_dir / ".openkb/hashes.json"
    value = json.loads(registry.read_text())
    value["hash-paper"]["raw_path"] = str(outside)
    registry.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        preview_removal(kb_dir, "hash-paper")
    assert outside.read_text() == "keep"


def test_retained_resources_include_kept_raw_and_empty_page(kb_dir):
    seed(kb_dir)
    page = kb_dir / "wiki/concepts/only.md"
    page.write_text("---\nsources: [summaries/paper.md]\n---\nKeep this\n")
    result = remove_document(kb_dir, "hash-paper", keep_raw=True, keep_empty=True)
    assert result.status == "removed"
    assert {"raw/paper.pdf", "wiki/concepts/only.md"} <= set(result.retained)
    assert all((kb_dir / path).exists() for path in result.retained)


def test_image_deletion_failure_cannot_commit_success(kb_dir):
    seed(kb_dir)
    images = kb_dir / "wiki/sources/images/paper"
    images.mkdir(parents=True)
    (images / "figure.png").write_bytes(b"image")
    import shutil

    original = shutil.rmtree

    def fail_images(path, *args, **kwargs):
        if Path(path) == images:
            raise PermissionError("cannot delete images")
        return original(path, *args, **kwargs)

    from openkb.mutation import RecoveryRequired

    with patch("shutil.rmtree", side_effect=fail_images):
        with pytest.raises((PermissionError, RecoveryRequired)):
            remove_document(kb_dir, "hash-paper")
    assert "hash-paper" in json.loads((kb_dir / ".openkb/hashes.json").read_text())
    assert (kb_dir / "raw/paper.pdf").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_api_removal_rejects_index_directory_pointing_outside_kb(kb_dir):
    from openkb.application.removal import run_remove_for_api

    blob = seed_index(kb_dir)
    outside = kb_dir.parent / "foreign-index"
    blob.parent.rename(outside)
    blob.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        run_remove_for_api(kb_dir, "hash-paper")
    assert (outside / blob.name).read_bytes() == b"indexed original"
    assert (kb_dir / "wiki/summaries/paper.md").exists()


def test_failed_final_rollback_keeps_committed_wiki_facts_in_blocked_receipt(kb_dir):
    from openkb.application.execution import ExecutionContext
    from openkb.mutation import MutationSnapshot, repair_marker
    from openkb.runtime.records import UnitIdentity, UnitResult
    from openkb.runtime.requests import RemoveDocument
    from openkb.runtime.worker import _execute

    seed_index(kb_dir)
    original_rollback = MutationSnapshot.rollback

    def fail_final_rollback(snapshot):
        if snapshot.operation == "remove-index-and-registry":
            raise OSError("restore failed")
        return original_rollback(snapshot)

    with (
        patch.object(HashRegistry, "remove_by_hash", side_effect=OSError("commit failed")),
        patch.object(MutationSnapshot, "rollback", fail_final_rollback),
    ):
        result = remove_document(kb_dir, "hash-paper")
    assert result.status == "blocked" and repair_marker(kb_dir).exists()
    assert "deleted: wiki/summaries/paper.md" in result.result.changes
    assert "knowledge_base_repair" in result.unfinished
    with patch("openkb.application.removal.remove_document", return_value=result):
        receipt = _execute(
            RemoveDocument("hash-paper", "confirmed"),
            UnitIdentity(task_id="a" * 32, unit_id="0", kb_dir=str(kb_dir), request_hash="b" * 64),
            ExecutionContext(),
        )
    assert receipt.status == "blocked" and receipt.halt
    assert receipt.changes and receipt.unfinished and receipt.error
    summary = json.loads(json.dumps(receipt.summary()))
    assert UnitResult.from_summary(summary).changes == receipt.changes
