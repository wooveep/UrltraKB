"""Reviewed aggregate effects survive withdrawal, stopping and partial failure."""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from publication_fixtures import publish_document

from ultrakb.application.batch_removal import (
    BatchRemovalPreview,
    execute_batch_removal,
    preview_batch_removal,
)
from ultrakb.application.document_actions import DocumentIdentity
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.query_views import resolve_query_views
from ultrakb.application.refresh import refresh_status
from ultrakb.application.views import view_scope
from ultrakb.config import LlmCredentialBundle
from ultrakb.file_state import file_versions
from ultrakb.locks import atomic_write_text, kb_ingest_lock
from ultrakb.runtime.records import UnitResult
from ultrakb.source_catalog import read_source


def shared_sources(kb, outside):
    """Actual publication/storage, with zero model sends and external originals."""
    outcomes = []
    for name in ("甲", "乙"):
        original = outside / f"{name}.md"
        original.write_text(f"# Policy {name}\n\nA seven-day review period.")
        paths = [f"summaries/{read_source(kb, item.source_id).doc_name}.md" for item in outcomes]
        outcomes.append(
            publish_document(
                kb,
                original,
                "A seven-day review period.",
                bundle=LlmCredentialBundle(api_key="unused-fixture"),
                shared_body="Common review policy.",
                shared_sources=paths + [f"summaries/{name}.md"],
            )
        )
    return tuple(DocumentIdentity("source", item.source_id) for item in outcomes)


@pytest.fixture
def selected(kb_dir, tmp_path):
    outside = tmp_path.parent / f"external-{tmp_path.name}"
    outside.mkdir()
    return shared_sources(kb_dir, outside)


def test_aggregate_withdrawal_preserves_external_originals_and_history(kb_dir, selected):
    reviewed = preview_batch_removal(kb_dir, selected)
    assert "concepts/shared.md" in reviewed.affected_pages
    assert "concepts/shared.md" in reviewed.lose_all_sources
    assert all(
        "concepts/shared.md" not in preview_batch_removal(kb_dir, (item,)).lose_all_sources
        for item in selected
    )
    assert BatchRemovalPreview.decode(reviewed.encode()) == reviewed
    before = file_versions(kb_dir, [kb_dir / ".ultrakb/knowledge/legacy/revisions"])
    context = ExecutionContext()
    result = execute_batch_removal(reviewed, context=context)
    assert result.status == "completed" and all(item.status == "completed" for item in result.items)
    assert all(read_source(kb_dir, item.value).removed for item in selected)
    assert before == file_versions(kb_dir, [kb_dir / ".ultrakb/knowledge/legacy/revisions"])
    assert all((kb_dir / path).exists() for path in reviewed.originals)
    assert all(
        (kb_dir.parent / f"external-{kb_dir.name}" / f"{name}.md").exists() for name in ("甲", "乙")
    )
    assert (
        "concepts/shared.md"
        in refresh_status(kb_dir, scope=view_scope(kb_dir, "legacy"))["needs_refresh"]
    )
    assert not resolve_query_views(kb_dir).has_evidence
    assert context.executor.calls == 0
    receipt = UnitResult("completed", removal_items=result.items)
    assert (
        UnitResult.from_summary(json.loads(json.dumps(receipt.summary()))).removal_items
        == result.items
    )


def test_stale_confirmation_changes_nothing(kb_dir, selected):
    reviewed = preview_batch_removal(kb_dir, selected)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(kb_dir / "wiki/concepts/new.md", "Human edits during review")
    result = execute_batch_removal(reviewed)
    assert result.status == "blocked" and all(item.status == "not_run" for item in result.items)
    assert not any(read_source(kb_dir, item.value).removed for item in selected)


def test_cancel_before_admission_keeps_every_source(kb_dir, selected):
    result = execute_batch_removal(
        preview_batch_removal(kb_dir, selected), context=ExecutionContext(cancelled=lambda: True)
    )
    assert result.status == "stopped" and all(item.status == "not_run" for item in result.items)
    assert not any(read_source(kb_dir, item.value).removed for item in selected)


def test_stop_after_one_retains_committed_result(kb_dir, selected):
    stopped = False

    def progress(event):
        nonlocal stopped
        if event.get("stage") == "removing":
            stopped = True

    result = execute_batch_removal(
        preview_batch_removal(kb_dir, selected),
        context=ExecutionContext(cancelled=lambda: stopped, on_event=progress),
    )
    assert result.status == "stopped"
    assert [item.status for item in result.items] == ["completed", "not_run"]
    assert read_source(kb_dir, result.items[0].identifier).removed
    assert not read_source(kb_dir, result.items[1].identifier).removed


@pytest.mark.parametrize("failure", ["write", "concurrent_change"])
def test_partial_failure_or_expanded_effect_stops_remaining(kb_dir, selected, failure):
    from ultrakb.application import batch_removal

    reviewed = preview_batch_removal(kb_dir, selected)
    actual = batch_removal.remove_document
    calls = []

    def remove(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise OSError("second transaction failed")
        return actual(*args, **kwargs)

    def progress(event):
        if failure == "concurrent_change" and event.get("stage") == "removing":
            atomic_write_text(kb_dir / "wiki/concepts/extra.md", "New dependency beyond approval")

    with patch.object(batch_removal, "remove_document", remove):
        result = execute_batch_removal(reviewed, context=ExecutionContext(on_event=progress))
    assert result.status == "partial" and [item.status for item in result.items] == [
        "completed",
        "failed",
    ]
    assert read_source(kb_dir, result.items[0].identifier).removed
    assert not read_source(kb_dir, result.items[1].identifier).removed
    assert len(calls) == (2 if failure == "write" else 1)


def test_native_batch_withdrawal_receipt(tmp_path):
    pytest.importorskip("PySide6")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_batch_removal.py",
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
    assert len(report["checks"]) == 4 and report["model_sends"] == 0
