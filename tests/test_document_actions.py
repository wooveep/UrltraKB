"""Document commands are qualified by saved bodies and the actual query collection."""

import json
import os
import subprocess
import sys

import pytest

from ultrakb.application.document_actions import DocumentIdentity, document_inventory


def test_source_and_legacy_identity_namespaces_do_not_collide():
    source = DocumentIdentity.from_inventory({"source_id": "a" * 32, "hash": "digest"})
    legacy = DocumentIdentity.from_inventory({"hash": "a" * 32})
    assert source.value == legacy.value and source != legacy
    assert len({source, legacy}) == 2


def test_native_documents_controls_and_states(tmp_path):
    pytest.importorskip("PySide6")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_documents_ux.py",
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
    record = json.loads((tmp_path / "gui/result.json").read_text())
    assert record["model_sends"] == 0 and len(record["checks"]) == 5


def test_source_replacement_retains_reading_and_marks_knowledge_pending(tmp_path, kb_dir):
    from publication_fixtures import fail_document, publish_document

    from ultrakb.config import LlmCredentialBundle
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    path = kb_dir / "raw/input.md"
    bundle = LlmCredentialBundle(api_key="unused-fixture")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(path, "# Original\n\nReadable source text.")
    result = publish_document(kb_dir, path, "Published summary", bundle=bundle)
    [row] = document_inventory(kb_dir)
    assert row.identity.value == result.source_id
    assert row.actions["read"].allowed and row.actions["ask"].allowed
    assert row.actions["recompile"].allowed and not row.actions["retry"].allowed
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(path, "# Replacement\n\nUpdated source text.")
    fail_document(kb_dir, path, bundle=bundle)
    [changed] = document_inventory(kb_dir)
    assert changed.identity == row.identity
    assert changed.actions["read"].allowed and changed.actions["retry"].allowed
    assert not changed.actions["recompile"].allowed
    assert "知识待更新" in changed.status


@pytest.mark.parametrize("reading", ["default", "live", "published"])
def test_document_inventory_retains_an_unstarted_workbook_sheet(kb_dir, reading):
    from focus_fixtures import partial_workbook

    from ultrakb.application.views import published_scope
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.knowledge_scope import legacy_scope

    source = partial_workbook(
        kb_dir, kb_dir / "raw/partial.xlsx", bundle=LlmCredentialBundle(api_key="unused-fixture")
    )
    scope = (
        published_scope(kb_dir)
        if reading == "published"
        else legacy_scope(kb_dir)
        if reading == "live"
        else None
    )
    rows = {row.identity.value: row for row in document_inventory(kb_dir, scope=scope)}
    assert {unit["name"]: unit["status"] for unit in rows[source].document["units"]} == {
        "Alpha": "completed",
        "Beta": "failed",
        "Gamma": "pending",
    }
    assert rows[source].actions["retry"].allowed
