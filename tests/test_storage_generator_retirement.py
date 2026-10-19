"""Retire abandoned unsealed plans without discarding real paid generator work."""

import json

import pytest
import pytest_asyncio
from stream_transport import StreamTransport
from test_storage_generators import generator_knowledge

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.generators import GenerationOptions, generate_artifact
from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.application.storage_maintenance import preview_root_retirement, retire_unused_roots
from ultrakb.application.views import storage_scope
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.execution_store import ExecutionStore


@pytest_asyncio.fixture
async def abandoned_plan(tmp_path):
    root, identity = generator_knowledge(tmp_path)
    context = ExecutionContext()
    scope = storage_scope(root, identity)
    options = GenerationOptions("skill", "explain-value", "Explain the recorded value.")
    skill = "---\nname: explain-value\ndescription: Explain the value.\n---\n# Value\n17\n"
    responses = [
        {
            "tool_calls": [
                {
                    "id": "write-skill",
                    "type": "function",
                    "function": {
                        "name": "write_skill_file",
                        "arguments": json.dumps({"path": "SKILL.md", "content": skill}),
                    },
                }
            ]
        },
        {"content": "Complete."},
    ]
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER fail_generator_seal BEFORE UPDATE ON ukb_generation_plans "
            "WHEN NEW.state = 'ready' BEGIN SELECT RAISE(ABORT, 'seal interrupted'); END"
        )
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        failed = await generate_artifact(root, options, scope=scope, context=context)
        assert failed.status == "failed", failed
        assert len(service.requests) == 2
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_generator_seal")
            [(plan, state)] = db.connection.execute(
                "SELECT plan_id, state FROM ukb_generation_plans"
            ).fetchall()
            assert state == "building"
            assert (
                db.connection.execute("SELECT count(*) FROM ukb_generation_files").fetchone()[0]
                == 2
            )
        yield root, identity, plan, context, scope, options, service, skill


@pytest.mark.asyncio
async def test_unsealed_plan_retires_in_batches_and_resumes_without_model_resends(abandoned_plan):
    root, identity, plan, context, scope, options, service, skill = abandoned_plan
    before = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    with storage_stage(root, identity, read_only=True) as db:
        accepted = db.connection.execute(
            "SELECT * FROM ukb_compile_steps ORDER BY rowid"
        ).fetchall()
    preview = preview_root_retirement(root, max_items=1)
    assert preview.candidate_generator_plans == 1
    assert preview.candidate_generator_files == 1 and preview.has_more
    total_plans = total_files = 0
    for _ in range(4):
        result = retire_unused_roots(root, max_items=1)
        assert result.status == "completed"
        assert result.retired_generator_files <= 1
        total_plans += result.retired_generator_plans
        total_files += result.retired_generator_files
        if not result.has_more:
            break
    assert (total_plans, total_files) == (1, 2)
    with storage_stage(root, identity, read_only=True) as db:
        assert not db.connection.execute(
            "SELECT 1 FROM ukb_generation_plans WHERE plan_id = ?", (plan,)
        ).fetchone()
        assert (
            db.connection.execute("SELECT * FROM ukb_compile_steps ORDER BY rowid").fetchall()
            == accepted
        )
    after = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    assert after.requests == before.requests
    assert after.task.budget == before.task.budget
    resumed = await generate_artifact(root, options, scope=scope, context=context)
    assert resumed.status == "completed", resumed
    assert len(service.requests) == 2
    assert (
        ArtifactStore(root, identity).read("output/skills/explain-value/SKILL.md") == skill.encode()
    )
    assert preview_root_retirement(root).candidate_generator_plans == 0
    assert preview_root_retirement(root).protected_generator_plans == 1
    assert retire_unused_roots(root).retired_generator_plans == 0


@pytest.mark.asyncio
async def test_active_budget_and_small_byte_budget_protect_unsealed_plan(abandoned_plan):
    root, identity, _, context, *_ = abandoned_plan
    assert preview_root_retirement(root, max_bytes=1).has_more
    assert retire_unused_roots(root, max_bytes=1).retired_generator_files == 0
    store = ExecutionStore(root, identity)
    resumed = store.begin(
        "model_task",
        fingerprint=context.executor.fingerprint,
        max_calls=context.executor.policy.max_calls,
        resume=context.storage_execution.execution_id,
    )
    preview = preview_root_retirement(root)
    assert preview.candidate_generator_plans == 0
    assert preview.protected_generator_plans == 1
    assert retire_unused_roots(root).retired_generator_files == 0
    store.finish(resumed.execution_id, state="failed", work_settled=True)
    assert preview_root_retirement(root).candidate_generator_plans == 1


@pytest.mark.asyncio
async def test_interrupted_plan_retirement_retries_without_orphaning_references(abandoned_plan):
    import sqlite3

    root, identity, _, *_ = abandoned_plan
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER fail_generator_retirement BEFORE DELETE ON ukb_generation_files "
            "BEGIN SELECT RAISE(ABORT, 'retirement interrupted'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="retirement interrupted"):
        retire_unused_roots(root)
    with storage_stage(root, identity) as db:
        assert db.connection.execute("SELECT state FROM ukb_generation_plans").fetchone() == (
            "retiring",
        )
        assert db.connection.execute("SELECT count(*) FROM ukb_generation_files").fetchone()[0] == 2
        db.connection.execute("DROP TRIGGER fail_generator_retirement")
    result = retire_unused_roots(root)
    assert (result.retired_generator_plans, result.retired_generator_files) == (1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["cli", "http", "desktop"])
async def test_three_adapters_retire_unsealed_plans_with_the_same_core_preserved(
    abandoned_plan, adapter, monkeypatch
):
    root, identity, _, context, _, _, service, _ = abandoned_plan
    before = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    if adapter == "cli":
        from click.testing import CliRunner

        from ultrakb.cli import cli

        result = CliRunner().invoke(cli, ["--kb-dir", str(root), "storage", "retire-roots"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["retired_generator_plans"] == 1
    elif adapter == "http":
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app
        from ultrakb.config import register_kb_alias

        monkeypatch.setenv("ULTRAKB_API_TOKEN", "generator-retirement-token")
        register_kb_alias("generator-retirement", root)
        request = {
            "kb": "generator-retirement",
            "kb_id": identity.kb_id,
            "generation": identity.generation,
        }
        headers = {"Authorization": "Bearer generator-retirement-token"}
        with TestClient(create_app()) as client:
            stale = client.post(
                "/api/v1/storage/retire-roots",
                json=request | {"generation": "stale"},
                headers=headers,
            )
            assert stale.status_code == 409
            result = client.post("/api/v1/storage/retire-roots", json=request, headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()["retired_generator_plans"] == 1
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
            assert dialog.overview.roots.candidate_generator_plans == 1
            assert "未封存生成计划" in dialog.body.toPlainText()
            dialog.retire_roots()
            until(lambda: dialog.last_result is not None and not dialog.busy)
            assert dialog.last_result.retired_generator_plans == 1
        finally:
            dialog.reject()
            app.processEvents()
    after = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    assert after.requests == before.requests and after.task.budget == before.task.budget
    assert len(service.requests) == 2
    assert preview_root_retirement(root).candidate_generator_plans == 0
