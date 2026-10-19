"""Refresh reviews have complete bounded pages and load full differences on selection."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_storage_generators import generator_knowledge

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.refresh import list_refresh_proposal_page, list_refresh_proposals
from ultrakb.application.removal import remove_document
from ultrakb.application.storage_execution import begin_storage_operation
from ultrakb.application.views import storage_scope
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.refresh_store import RefreshStore, refresh_targets

pytestmark = pytest.mark.integration


def retained_refresh_reviews(tmp_path):
    root, identity = generator_knowledge(tmp_path)
    knowledge, store = KnowledgeStore(root, identity), RefreshStore(root, identity)
    previous = storage_scope(root, identity).knowledge
    knowledge.publish(
        knowledge.prepare_changes(previous, {"concepts/value.md": b"# Current value\n\n17\n"})
    )
    current = storage_scope(root, identity)
    removed = remove_document(root, current.knowledge.input_units()[0].source_id, scope=current)
    assert removed.status == "removed", removed
    current = storage_scope(root, identity)
    generation = refresh_targets(current.knowledge).generation
    context = ExecutionContext(storage_identity=identity)
    with begin_storage_operation(root, context, "prepare-refresh-review-fixture"):
        prepared = knowledge.prepare_changes(
            current.knowledge,
            {"concepts/value.md": b"# Reviewed replacement\n\n17\n"},
            refresh_generation=generation,
        )
        expected = store.save(
            prepared, conflicts=("concepts/value.md",), execution=context.storage_execution
        )["proposal_id"]
        for index in range(64):
            prepared = knowledge.prepare_changes(
                previous,
                {"concepts/value.md": f"# Superseded replacement {index}\n".encode()},
                refresh_generation=generation,
            )
            store.save(
                prepared, conflicts=("concepts/value.md",), execution=context.storage_execution
            )
        context._storage_result = "completed"
    return root, identity, current, expected


def test_refresh_review_tail_is_accessible_through_shared_cli_http_and_qt_io(tmp_path, monkeypatch):
    root, identity, scope, expected = retained_refresh_reviews(tmp_path)
    (root / ".ultrakb/config.yaml").unlink()
    first = list_refresh_proposal_page(root, scope=scope)
    assert len(first.proposals) == 63 and first.next_after
    assert all(item["status"] == "superseded" and "diff" not in item for item in first.proposals)
    tail = list_refresh_proposal_page(root, scope=scope, after=first.next_after)
    assert tail.next_after is None and len(tail.proposals) == 2
    assert tail.proposals[-1]["proposal_id"] == expected
    assert tail.proposals[-1]["status"] == "awaiting_confirmation"
    assert len(list_refresh_proposals(root, scope=scope)) == 65

    from click.testing import CliRunner

    import ultrakb.cli as cli

    monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
    monkeypatch.setattr(cli, "_selected_scope", lambda *args, **kwargs: scope)
    shown = CliRunner().invoke(cli.cli, ["refresh", "proposals"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["next_after"] == first.next_after
    shown = CliRunner().invoke(cli.cli, ["refresh", "proposals", "--after", first.next_after])
    assert shown.exit_code == 0 and expected in shown.output, shown.output

    from fastapi.testclient import TestClient

    import ultrakb.api_refresh as refresh
    import ultrakb.api_views as views
    from ultrakb.api import create_app

    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
    monkeypatch.setattr(refresh, "_resolve_kb", lambda *args: root)
    monkeypatch.setattr(views, "view_scope", lambda *args, **kwargs: scope)
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/refresh/proposals", params={"kb": str(root)})
        assert response.status_code == 200 and len(response.json()) == 63, response.text
        cursor = response.headers["X-UltraKB-Next-After"]
        response = client.get(
            "/api/v1/refresh/proposals", params={"kb": str(root), "after": cursor}
        )
        assert response.status_code == 200 and response.json()[-1]["proposal_id"] == expected
        invalid = client.get(
            "/api/v1/refresh/proposals", params={"kb": str(root), "after": "0" * 32}
        )
        assert invalid.status_code == 400

    pytest.importorskip("PySide6.QtWidgets")
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            "sys.path.insert(0, str(Path(sys.argv[1]).parent)); "
            "runpy.run_path(sys.argv[1])['_exercise_refresh_desktop'](Path(sys.argv[2]))",
            str(Path(__file__).resolve()),
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert process.returncode == 0, process.stdout + process.stderr


def _exercise_refresh_desktop(root):
    import logging
    import time

    from PySide6.QtWidgets import QApplication, QWidget

    from ultrakb.desktop.io import LocalIO
    from ultrakb.desktop.refresh import RefreshDialog
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
    dialog = RefreshDialog(window, root)
    assert dialog.scope is window.read_scope
    window.read_scope = None

    def until(predicate, seconds=8):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), "Desktop refresh I/O did not finish"

    try:
        until(lambda: dialog.more_button.isEnabled())
        assert "来源撤回" in dialog.reasons.toPlainText()
        assert dialog.proposals.count() == 0 and not dialog.accept_button.isEnabled()
        dialog.load_more()
        until(lambda: dialog.proposals.count() == 1)
        assert not dialog.more_button.isEnabled()
        dialog.proposals.setCurrentRow(0)
        until(lambda: "Reviewed replacement" in dialog.diff.toPlainText())
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
        dialog.close()
        window.io.stop()
        until(window.io.stopped, seconds=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [False, True])
async def test_http_review_cancellation_joins_the_storage_reader_waiting_on_reclamation(
    tmp_path, monkeypatch, detail
):
    import asyncio
    from contextlib import contextmanager
    from threading import Event

    from httpx import ASGITransport, AsyncClient

    import ultrakb.api_refresh as refresh
    import ultrakb.api_views as views
    from ultrakb.api import create_app
    from ultrakb.storage import coordination

    root, identity = generator_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    context = ExecutionContext(storage_identity=identity)
    with begin_storage_operation(root, context, "prepare-cancelled-refresh-review"):
        prepared = KnowledgeStore(root, identity).prepare_changes(
            scope.knowledge,
            {"concepts/value.md": b"# Reviewed replacement\n\n17\n"},
            refresh_generation=refresh_targets(scope.knowledge).generation,
        )
        review = RefreshStore(root, identity).save(
            prepared, conflicts=("concepts/value.md",), execution=context.storage_execution
        )
        context._storage_result = "completed"
    entered, finished = Event(), Event()
    stage = coordination.storage_stage

    @contextmanager
    def observed(*args, **kwargs):
        entered.set()
        try:
            with stage(*args, **kwargs) as db:
                yield db
        finally:
            finished.set()

    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
    monkeypatch.setattr(refresh, "_resolve_kb", lambda *args: root)
    monkeypatch.setattr(views, "view_scope", lambda *args, **kwargs: scope)
    monkeypatch.setattr(coordination, "storage_stage", observed)
    monkeypatch.setattr("ultrakb.storage.refresh_store.storage_stage", observed)
    with coordination.try_reclamation(root, identity) as db:
        assert db is not None
        async with AsyncClient(
            transport=ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/api/v1/refresh/proposal",
                    json={"kb": str(root), "proposal_id": review["proposal_id"]},
                )
                if detail
                else client.get("/api/v1/refresh/proposals", params={"kb": str(root)})
            )
            assert await asyncio.to_thread(entered.wait, 3)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert finished.is_set(), "HTTP cancellation returned before its storage reader stopped"
