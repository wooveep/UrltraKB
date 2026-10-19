"""Interrupted compiler preparation may retire without losing accepted paid work."""

import json

import pytest
import pytest_asyncio
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.storage_maintenance import preview_root_retirement, retire_unused_roots
from ultrakb.application.views import storage_scope
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.maintenance import Maintenance


@pytest_asyncio.fixture(params=["draft", "sealed"])
async def interrupted_preparation(tmp_path, request):
    root, identity, unit = compile_input(tmp_path)
    scope = storage_scope(root, identity)
    context = ExecutionContext()
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    with storage_stage(root, identity) as db:
        if request.param == "draft":
            db.connection.execute(
                "CREATE TRIGGER fail_revision_preparation BEFORE INSERT ON ukb_revision_files "
                "BEGIN SELECT RAISE(ABORT, 'draft interrupted'); END"
            )
        else:
            db.connection.execute(
                "CREATE TRIGGER fail_revision_preparation BEFORE UPDATE ON ukb_knowledge_revisions "
                "WHEN NEW.state = 'ready' BEGIN SELECT RAISE(ABORT, 'seal interrupted'); END"
            )
    with StreamTransport(
        [{"content": summary}, {"content": json.dumps({"concepts": [], "entities": []})}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, context=context, scope=scope
        )
        assert result.status == "blocked", result
        assert len(service.requests) == 2
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_revision_preparation")
            [(revision, state)] = db.connection.execute(
                "SELECT revision_id, state FROM ukb_knowledge_revisions WHERE state != 'published'"
            ).fetchall()
            assert state == request.param
        yield root, identity, unit, context, scope, service, revision


@pytest.mark.asyncio
async def test_aborted_revision_retires_then_resumes_the_original_compiler_without_resends(
    interrupted_preparation,
):
    root, identity, unit, context, scope, service, revision = interrupted_preparation
    before = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    old = KnowledgeStore(root, identity).bind()
    with storage_stage(root, identity, read_only=True) as db:
        accepted = db.connection.execute(
            "SELECT * FROM ukb_compile_steps ORDER BY rowid"
        ).fetchall()
    assert preview_root_retirement(root).candidate_preparations == 1
    tiny = preview_root_retirement(root, max_bytes=1)
    assert tiny.candidate_preparations == 0 and tiny.has_more
    assert retire_unused_roots(root, max_bytes=1).retired_preparation_rows == 0
    retired = 0
    for _ in range(100):
        result = retire_unused_roots(root, max_items=2)
        retired += result.retired_preparations
        with storage_stage(root, identity, read_only=True) as db:
            assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert Maintenance(root, identity).collect(max_items=2).status == "completed"
        assert old.read("index.md")
        if not result.has_more:
            break
    assert retired == 1
    with storage_stage(root, identity, read_only=True) as db:
        assert not db.connection.execute(
            "SELECT 1 FROM ukb_knowledge_revisions WHERE revision_id = ?", (revision,)
        ).fetchone()
        assert (
            db.connection.execute("SELECT * FROM ukb_compile_steps ORDER BY rowid").fetchall()
            == accepted
        )
    after = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    assert after.requests == before.requests and after.task.budget == before.task.budget
    resumed = await recompile_document(
        root, unit.source_id, unit_id=unit.unit_id, context=context, scope=scope
    )
    assert resumed.status == "compiled", resumed
    assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_complete_ready_candidate_remains_protected(interrupted_preparation):
    root, identity, _, _, _, _, _ = interrupted_preparation
    store = KnowledgeStore(root, identity)
    prepared = store.prepare_changes(
        store.bind(), {"index.md": b"A complete unpublished candidate"}
    )
    for _ in range(10):
        if not retire_unused_roots(root).has_more:
            break
    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute(
            "SELECT state FROM ukb_knowledge_revisions WHERE revision_id = ?",
            (prepared.revision_id,),
        ).fetchone() == ("ready",)
    assert store.publish(prepared).publication_id == prepared.publication_id


@pytest.mark.asyncio
async def test_interrupted_child_retirement_keeps_reference_closure(interrupted_preparation):
    import sqlite3

    root, identity, _, _, _, _, revision = interrupted_preparation
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER fail_preparation_retirement BEFORE DELETE ON ukb_revision_navigation "
            "BEGIN SELECT RAISE(ABORT, 'retirement interrupted'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="retirement interrupted"):
        retire_unused_roots(root)
    with storage_stage(root, identity) as db:
        assert db.connection.execute(
            "SELECT state FROM ukb_knowledge_revisions WHERE revision_id = ?", (revision,)
        ).fetchone() == ("retiring",)
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        db.connection.execute("DROP TRIGGER fail_preparation_retirement")
    assert Maintenance(root, identity).collect().status == "completed"
    for _ in range(10):
        if not retire_unused_roots(root).has_more:
            break
    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not db.connection.execute(
            "SELECT 1 FROM ukb_knowledge_revisions WHERE revision_id = ?", (revision,)
        ).fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted_preparation", ["sealed"], indirect=True)
@pytest.mark.parametrize("adapter", ["cli", "http", "desktop"])
async def test_three_adapters_finish_the_same_abandoned_preparation(
    interrupted_preparation, adapter, monkeypatch
):
    root, identity, _, context, _, service, _ = interrupted_preparation
    before = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    retired = 0
    if adapter == "cli":
        from click.testing import CliRunner

        from ultrakb.cli import cli

        for _ in range(10):
            result = CliRunner().invoke(cli, ["--kb-dir", str(root), "storage", "retire-roots"])
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output)
            retired += payload["retired_preparations"]
            if not payload["has_more"]:
                break
    elif adapter == "http":
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app
        from ultrakb.config import register_kb_alias

        monkeypatch.setenv("ULTRAKB_API_TOKEN", "preparation-retirement-token")
        register_kb_alias("preparation-retirement", root)
        request = {
            "kb": "preparation-retirement",
            "kb_id": identity.kb_id,
            "generation": identity.generation,
        }
        headers = {"Authorization": "Bearer preparation-retirement-token"}
        with TestClient(create_app()) as client:
            for _ in range(10):
                result = client.post("/api/v1/storage/retire-roots", json=request, headers=headers)
                assert result.status_code == 200, result.text
                retired += result.json()["retired_preparations"]
                if not result.json()["has_more"]:
                    break
    else:
        import time

        from ultrakb.desktop.storage_maintenance import StorageMaintenanceDialog

        widgets = pytest.importorskip("PySide6.QtWidgets")
        app = widgets.QApplication.instance() or widgets.QApplication([])
        dialog = StorageMaintenanceDialog(root)

        def until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert predicate(), dialog.status.text()

        try:
            until(lambda: dialog.overview is not None and not dialog.busy)
            assert "中断的编译准备" in dialog.body.toPlainText()
            for _ in range(10):
                dialog.last_result = None
                dialog.retire_roots()
                until(lambda: dialog.last_result is not None and not dialog.busy)
                retired += dialog.last_result.retired_preparations
                if not dialog.last_result.has_more:
                    break
        finally:
            dialog.reject()
            app.processEvents()
    assert retired == 1
    after = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    assert after.requests == before.requests and after.task.budget == before.task.budget
    assert len(service.requests) == 2
