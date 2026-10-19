"""Fresh applicability and CAS protect the explicit checkbox subset."""

import json
import os
import subprocess
import sys

import pytest

from ultrakb.application.document_actions import DocumentIdentity, document_inventory
from ultrakb.application.document_batches import (
    DocumentBatchChanged,
    preview_document_batch,
    validate_document_batch,
)


def test_native_checkbox_identity_and_shortcut_boundaries(tmp_path):
    pytest.importorskip("PySide6")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_document_selection.py",
            "--headless",
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
    assert len(report["checks"]) == 4 and report["service"] == "no model calls"


def test_review_rejects_changed_knowledge_and_removed_target(kb_dir):
    from publication_fixtures import publish_document

    from ultrakb.application.removal import remove_document
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.desktop.verification_data import publish_pages
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    path = kb_dir / "raw/manual.md"
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(path, "# Rules\n\nSaved operating policy.")
    publish_document(
        kb_dir, path, "Saved summary", bundle=LlmCredentialBundle(api_key="unused-fixture")
    )
    [row] = document_inventory(kb_dir)
    missing = DocumentIdentity("source", "f" * 32)
    preview = preview_document_batch(kb_dir, (row.identity, row.identity, missing))
    assert preview.selected == tuple(sorted({row.identity, missing}))
    assert preview.approved == (row.identity,) and len(preview.skipped) == 1
    assert validate_document_batch(preview) == preview
    publish_pages(kb_dir, {"concepts/new.md": b"Changed while confirming"})
    with pytest.raises(DocumentBatchChanged):
        validate_document_batch(preview)
    with pytest.raises(ValueError, match="typed"):
        preview_document_batch(kb_dir, (row.identity.value,))
    refreshed = preview_document_batch(kb_dir, (row.identity,))
    assert remove_document(kb_dir, row.identity.value).status == "removed"
    with pytest.raises(DocumentBatchChanged):
        validate_document_batch(refreshed)
    removed = preview_document_batch(kb_dir, (row.identity,))
    assert not removed.approved and len(removed.skipped) == 1
