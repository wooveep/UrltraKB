"""Explicit candidate disposal releases roots without discarding accepted model work."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.storage_maintenance import (
    collect_garbage,
    discard_storage_candidate,
    list_storage_candidates,
    retire_unused_roots,
)
from ultrakb.application.views import storage_scope
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.knowledge_store import KnowledgeStore


@pytest.mark.asyncio
async def test_discarded_import_can_resume_accepted_compilation_without_resending(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER fail_publish BEFORE INSERT ON ukb_publications "
            "BEGIN SELECT RAISE(ABORT, 'publication interrupted'); END"
        )
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    with StreamTransport(
        [{"content": summary}, {"content": json.dumps({"concepts": [], "entities": []})}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        failed = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, context=context, scope=scope
        )
        assert failed.status == "blocked" and len(service.requests) == 2
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_publish")
        before = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
        old = KnowledgeStore(root, identity).bind()
        [candidate] = list_storage_candidates(root).items
        assert candidate.kind == "import" and candidate.protected_reason is None
        assert (
            discard_storage_candidate(
                root, candidate.candidate_id, version="0" * 64, identity=identity
            ).status
            == "conflict"
        )
        assert (
            discard_storage_candidate(
                root, candidate.candidate_id, version=candidate.version, identity=identity
            ).status
            == "discarded"
        )
        for _ in range(100):
            result = retire_unused_roots(root, max_items=2)
            assert collect_garbage(root, max_items=2).status == "completed"
            assert old.read("index.md")
            with storage_stage(root, identity, read_only=True) as db:
                assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
            if not result.has_more:
                break
        assert list_storage_candidates(root).items == ()
        after = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
        assert after.requests == before.requests and after.task.budget == before.task.budget
        resumed = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, context=context, scope=scope
        )
        assert resumed.status == "compiled" and len(service.requests) == 2


@pytest.mark.asyncio
async def test_discarding_an_import_review_group_never_resurrects_an_older_copy(tmp_path):
    from test_storage_import_proposals import compiler_responses, reviewed_import

    from ultrakb.application.proposals import accept_proposal, list_proposals
    from ultrakb.storage.wiki_projection import ProjectionStore

    with StreamTransport(compiler_responses()) as service:
        root, identity, _, scope, _, _, original = await reviewed_import(tmp_path, service)
        ProjectionStore(root, identity).sync()
        refreshed = accept_proposal(
            root, original.proposal_id, version=original.version, scope=scope
        )
        latest = refreshed.units[0].proposal_id
        assert latest != original.proposal_id
        candidates = {item.candidate_id: item for item in list_storage_candidates(root).items}
        assert set(candidates) == {latest, original.proposal_id}
        assert (
            discard_storage_candidate(
                root, latest, version=candidates[latest].version, identity=identity
            ).status
            == "discarded"
        )
        assert list_proposals(root, scope=scope) == ()
        for _ in range(100):
            retired = retire_unused_roots(root, max_items=2)
            assert collect_garbage(root).status == "completed"
            if not retired.has_more:
                break
        assert list_storage_candidates(root).items == ()
        assert b"human annotation" in scope.knowledge.read("summaries/fixture.md")
        assert len(service.requests) == 4


def test_discard_protects_active_work_busy_readers_and_published_history(tmp_path):
    from ultrakb.application.storage_execution import begin_storage_operation
    from ultrakb.storage.database import DatabaseIdentity, StorageFormatError

    root, identity, _ = compile_input(tmp_path)
    knowledge = KnowledgeStore(root, identity)
    before = knowledge.bind()
    prepared = knowledge.prepare_changes(before, {"index.md": b"A complete candidate"})
    [candidate] = list_storage_candidates(root).items
    context = ExecutionContext(storage_identity=identity)
    with begin_storage_operation(root, context, "reviewing-candidate"):
        assert list_storage_candidates(root).items[0].protected_reason == "active_execution"
        assert (
            discard_storage_candidate(
                root, candidate.candidate_id, version=candidate.version, identity=identity
            ).blocked_reason
            == "active_execution"
        )
    with storage_stage(root, identity, read_only=True):
        assert (
            discard_storage_candidate(
                root, candidate.candidate_id, version=candidate.version, identity=identity
            ).status
            == "busy"
        )
    with pytest.raises(StorageFormatError):
        discard_storage_candidate(
            root,
            candidate.candidate_id,
            version=candidate.version,
            identity=DatabaseIdentity(identity.kb_id, "0" * 32),
        )
    knowledge.publish(prepared)
    assert (
        discard_storage_candidate(
            root, candidate.candidate_id, version=candidate.version, identity=identity
        ).blocked_reason
        == "retained_publication"
    )
    assert before.read("index.md") and knowledge.bind().read("index.md") == b"A complete candidate"


@pytest.mark.parametrize("adapter", ["cli", "http", "desktop"])
def test_candidate_discard_is_available_through_the_three_real_adapters(
    tmp_path, adapter, monkeypatch
):
    root, identity, _ = compile_input(tmp_path)
    store = KnowledgeStore(root, identity)
    prepared = store.prepare_changes(store.bind(), {"index.md": b"A complete candidate"})
    if adapter == "cli":
        from click.testing import CliRunner

        from ultrakb.cli import cli

        runner = CliRunner()
        listed = runner.invoke(cli, ["--kb-dir", str(root), "storage", "candidates"])
        assert listed.exit_code == 0, listed.output
        [candidate] = json.loads(listed.output)["items"]
        discarded = runner.invoke(
            cli,
            [
                "--kb-dir",
                str(root),
                "storage",
                "discard-candidate",
                candidate["candidate_id"],
                "--version",
                candidate["version"],
                "--kb-id",
                identity.kb_id,
                "--generation",
                identity.generation,
            ],
        )
        assert discarded.exit_code == 0, discarded.output
        assert json.loads(discarded.output)["status"] == "discarded"
    elif adapter == "http":
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app
        from ultrakb.config import register_kb_alias

        register_kb_alias("candidate-discard", root)
        monkeypatch.setenv("ULTRAKB_API_TOKEN", "candidate-token")
        headers = {"Authorization": "Bearer candidate-token"}
        with TestClient(create_app()) as client:
            listed = client.get(
                "/api/v1/storage/candidates", params={"kb": "candidate-discard"}, headers=headers
            )
            assert listed.status_code == 200, listed.text
            [candidate] = listed.json()["items"]
            response = client.post(
                "/api/v1/storage/discard-candidate",
                json={
                    "kb": "candidate-discard",
                    "kb_id": identity.kb_id,
                    "generation": identity.generation,
                    "candidate_id": candidate["candidate_id"],
                    "version": candidate["version"],
                },
                headers=headers,
            )
            assert response.status_code == 200 and response.json()["status"] == "discarded", (
                response.text
            )
    else:
        import time

        from ultrakb.desktop.storage_candidates import StorageCandidatesDialog

        widgets = pytest.importorskip("PySide6.QtWidgets")
        app = widgets.QApplication.instance() or widgets.QApplication([])
        dialog = StorageCandidatesDialog(root, identity)
        monkeypatch.setattr(
            widgets.QMessageBox, "question", lambda *a, **k: widgets.QMessageBox.Yes
        )

        def until(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert predicate(), dialog.status.text()

        try:
            until(lambda: dialog.list.count() == 1 and not dialog.busy)
            dialog.list.setCurrentRow(0)
            dialog.discard_selected()
            until(lambda: dialog.last_result is not None and not dialog.busy)
            assert dialog.last_result.status == "discarded"
        finally:
            dialog.reject()
            app.processEvents()
    assert list_storage_candidates(root).items == ()
    for _ in range(20):
        if not retire_unused_roots(root).has_more:
            break
    assert collect_garbage(root).status == "completed"
    assert prepared.revision_id != store.bind().publication.revision_id


@pytest.mark.asyncio
async def test_discarded_artifact_plan_keeps_the_paid_generation_for_retry(tmp_path):
    from test_storage_generators import generator_knowledge

    from ultrakb.application.generators import (
        GenerationOptions,
        generate_artifact,
        preview_generation,
    )

    root, identity = generator_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    preview = preview_generation(root, "skill", "explain-value", scope=scope)
    options = GenerationOptions(
        "skill", "explain-value", "Explain the value.", version=preview.version
    )
    skill = "---\nname: explain-value\ndescription: Explain the value.\n---\n# Value\n\n17\n"
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
        {"content": "Skill delivered."},
    ]
    with storage_stage(root, identity) as db:
        db.connection.execute(
            "CREATE TRIGGER fail_artifact BEFORE INSERT ON ukb_artifact_revisions "
            "BEGIN SELECT RAISE(ABORT, 'artifact interrupted'); END"
        )
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        failed = await generate_artifact(root, options, scope=scope, context=context)
        assert failed.status != "completed" and len(service.requests) == 2
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_artifact")
        [candidate] = list_storage_candidates(root).items
        assert candidate.kind == "generator-plan"
        assert (
            discard_storage_candidate(
                root,
                candidate.candidate_id,
                version=candidate.version,
                kind=candidate.kind,
                identity=identity,
            ).status
            == "discarded"
        )
        for _ in range(20):
            if not retire_unused_roots(root, max_items=2).has_more:
                break
        assert collect_garbage(root).status == "completed"
        resumed = await generate_artifact(root, options, scope=scope, context=context)
        assert resumed.status == "completed" and len(service.requests) == 2, resumed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["refresh", "lint"])
async def test_other_complete_candidates_release_review_roots_and_reuse_accepted_work(
    tmp_path, operation
):
    from test_storage_lint import lint_knowledge
    from test_storage_refresh import compiled, responses

    from ultrakb.application.maintenance import LintOptions, check_knowledge, preview_link_repair
    from ultrakb.application.refresh import refresh_knowledge_view

    root, identity = lint_knowledge(tmp_path) if operation == "lint" else (None, None)
    context = ExecutionContext()
    with StreamTransport([*responses(), *responses()]) as service:
        if operation == "refresh":
            root, identity, unit = compile_input(tmp_path)
            (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
            scope = await compiled(root, identity, unit)
        else:
            scope = storage_scope(root, identity)
            version, _ = preview_link_repair(root, scope=scope)

        async def run():
            if operation == "refresh":
                return await refresh_knowledge_view(root, scope=scope, context=context)
            return await check_knowledge(
                root,
                LintOptions(fix=True, semantic=False, version=version),
                scope=scope,
                context=context,
            )

        with storage_stage(root, identity) as db:
            db.connection.execute(
                "CREATE TRIGGER fail_publish BEFORE INSERT ON ukb_publications "
                "BEGIN SELECT RAISE(ABORT, 'publication interrupted'); END"
            )
        failed = await run()
        assert failed.status == "failed", failed
        with storage_stage(root, identity) as db:
            db.connection.execute("DROP TRIGGER fail_publish")
        [candidate] = list_storage_candidates(root).items
        assert candidate.kind == operation
        sends = len(service.requests)
        before = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
        assert (
            discard_storage_candidate(
                root, candidate.candidate_id, version=candidate.version, identity=identity
            ).status
            == "discarded"
        )
        for _ in range(50):
            retired = retire_unused_roots(root, max_items=2)
            assert collect_garbage(root).status == "completed"
            if not retired.has_more:
                break
        after = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
        assert before.requests == after.requests and before.task.budget == after.task.budget
        assert (await run()).status == "completed"
        assert len(service.requests) == sends
