"""Retained reviews remain discoverable through bounded lists and actual desktop I/O."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from stream_transport import StreamTransport
from test_storage_import_proposals import compiler_responses, manual_baseline

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.proposals import list_proposal_page, list_proposals
from ultrakb.application.storage_execution import begin_storage_operation
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.import_candidates import ImportCandidates
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.pending_store import PendingStore

pytestmark = pytest.mark.integration


def retain_reviews(root, identity, original):
    catalog = CatalogStore(root, identity)
    units = [
        catalog.plan_unit(original.processing_id, key=f"body-{i}", doc_name=f"fixture-{i}")
        for i in range(64)
    ]
    scope = storage_scope(root, identity)
    files = {path: scope.knowledge.read(path) for path in scope.knowledge.files()}
    files["summaries/fixture.md"] = b"# Model revision replaces the human annotation\n"
    pending, candidates = PendingStore(root, identity), ImportCandidates(root, identity)
    context, records = ExecutionContext(storage_identity=identity), []
    knowledge = KnowledgeStore(root, identity)
    with begin_storage_operation(root, context, "prepare-import-reviews"):
        for unit in units:
            work = pending.enqueue(
                unit.source_revision_id,
                unit_revision_id=unit.unit_revision_id,
                root_import_id=context.storage_execution.model_budget_id,
            )
            work = pending.start(pending.claim(intent_id=work.intent_id))
            revision = knowledge.prepare_revision(
                files,
                expected=scope.publication_id,
                unit_revisions=(original.unit_revision_id, unit.unit_revision_id),
                import_work=work,
            )
            candidate = candidates.retain(revision, scope.knowledge, context.storage_execution)
            records.append((candidate["proposal_id"], work))
        context._storage_result = "completed"
    return scope, records


@pytest.mark.asyncio
async def test_review_pages_are_complete_through_shared_cli_http_and_real_qt_io(
    tmp_path, monkeypatch
):
    with StreamTransport(compiler_responses()) as service:
        root, identity, original, _ = await manual_baseline(tmp_path, service)
    scope, records = retain_reviews(root, identity, original)
    first = list_proposal_page(root, scope=scope)
    assert len(first.proposals) == 63 and first.next_after
    last = list_proposal_page(root, scope=scope, after=first.next_after)
    assert [item.proposal_id for item in last.proposals] == [records[0][0]]
    assert last.next_after is None
    assert len(list_proposals(root, scope=scope)) == 64
    # List summaries contain no full review body; selection loads it separately.
    assert all(item.diff == "" for item in first.proposals)

    from click.testing import CliRunner

    import ultrakb.cli as cli

    monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
    monkeypatch.setattr(cli, "_selected_scope", lambda *args, **kwargs: scope)
    shown = CliRunner().invoke(cli.cli, ["proposals", "list"])
    assert shown.exit_code == 0 and first.next_after in shown.output, shown.output
    remaining = CliRunner().invoke(cli.cli, ["proposals", "list", "--after", first.next_after])
    assert remaining.exit_code == 0 and records[0][0] in remaining.output, remaining.output

    from fastapi.testclient import TestClient

    import ultrakb.api_documents_router as documents
    import ultrakb.api_views as views
    from ultrakb.api import create_app

    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
    monkeypatch.setattr(documents, "_resolve_kb", lambda *args: root)
    monkeypatch.setattr(views, "view_scope", lambda *args, **kwargs: scope)
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/proposals", params={"kb": str(root), "view_id": "legacy"})
        assert response.status_code == 200 and len(response.json()) == 63, response.text
        cursor = response.headers["X-UltraKB-Next-After"]
        response = client.get(
            "/api/v1/proposals", params={"kb": str(root), "view_id": "legacy", "after": cursor}
        )
        assert response.status_code == 200 and response.json()[0]["proposal_id"] == records[0][0]
        invalid = client.get(
            "/api/v1/proposals", params={"kb": str(root), "view_id": "legacy", "after": "0" * 32}
        )
        assert invalid.status_code == 400

    pytest.importorskip("PySide6.QtWidgets")
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "sys.path.insert(0, str(Path(sys.argv[1]).parent)); "
            "runpy.run_path(sys.argv[1])['_exercise_review_desktop'](Path(sys.argv[2]))",
            str(Path(__file__).resolve()),
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert process.returncode == 0, process.stdout + process.stderr

    # Superseded entries at the front cannot conceal an older current review.
    pending = PendingStore(root, identity)
    for _, work in records[1:]:
        pending.finish(work, status="blocked")
    assert [item.proposal_id for item in list_proposals(root, scope=scope)] == [records[0][0]]


def _exercise_review_desktop(root):
    import logging
    import time

    from PySide6.QtWidgets import QApplication, QWidget

    from ultrakb.desktop.io import LocalIO
    from ultrakb.desktop.proposals import ProposalsDialog
    from ultrakb.knowledge_scope import live_scope
    from ultrakb.storage.coordination import try_reclamation
    from ultrakb.storage.database import open_database

    logging.disable(logging.CRITICAL)
    with open_database(root, read_only=True) as db:
        identity = db.identity
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.scope = live_scope(root)
    window.read_scope = storage_scope(root, identity)
    window.io = LocalIO(window)
    dialog = ProposalsDialog(window, root)
    assert dialog.scope is window.read_scope
    window.read_scope = None

    def until(predicate, seconds=8):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), "Desktop review I/O did not finish"

    try:
        until(lambda: dialog.list.count() == 63)
        assert dialog.more_button.isEnabled() and not dialog.accept_button.isEnabled()
        dialog.load_more()
        until(lambda: dialog.list.count() == 64)
        assert not dialog.more_button.isEnabled()
        dialog.list.setCurrentRow(63)
        until(lambda: "reviewed human annotation" in dialog.diff.toPlainText())
        assert dialog.accept_button.isEnabled()
        until(window.io.stopped)
        with try_reclamation(root, identity) as db:
            assert db is not None
            dialog.reload()
            end = time.monotonic() + 0.15
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.01)
            assert not window.io.stopped()
            dialog.close()
            window.io.stop()
            until(window.io.stopped, seconds=2)
    finally:
        window.io.stop()
        until(window.io.stopped, seconds=2)
