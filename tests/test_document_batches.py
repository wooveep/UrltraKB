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

    from ultrakb.config import LlmCredentialBundle
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.state import HashRegistry

    path = kb_dir / "raw/manual.md"
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(path, "# Rules\n\nSaved operating policy.")
    publish_document(
        kb_dir, path, "Saved summary", bundle=LlmCredentialBundle(api_key="unused-fixture")
    )
    [row] = document_inventory(kb_dir)
    missing = DocumentIdentity("legacy", "f" * 64)
    preview = preview_document_batch(kb_dir, (row.identity, row.identity, missing))
    assert preview.selected == tuple(sorted({row.identity, missing}))
    assert preview.approved == (row.identity,) and len(preview.skipped) == 1
    assert validate_document_batch(preview) == preview
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(kb_dir / "wiki/concepts/new.md", "Changed while confirming")
    with pytest.raises(DocumentBatchChanged):
        validate_document_batch(preview)
    with pytest.raises(ValueError, match="typed"):
        preview_document_batch(kb_dir, (row.identity.value,))
    # Even an unusual legacy key cannot be mistaken for the new Source ID.
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        HashRegistry(kb_dir / ".ultrakb/hashes.json").add(
            row.identity.value, {"name": "collision.md", "type": "md"}
        )
    collision = preview_document_batch(
        kb_dir, (row.identity, DocumentIdentity("legacy", row.identity.value))
    )
    assert not collision.approved and len(collision.skipped) == 2
