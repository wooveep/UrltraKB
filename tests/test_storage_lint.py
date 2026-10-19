"""Maintenance reads fixed knowledge and commits repairs with its report."""

from pathlib import Path

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_bases import get_kb_status
from ultrakb.application.maintenance import LintOptions, check_knowledge, preview_link_repair
from ultrakb.application.views import storage_scope
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def test_structural_lint_reads_committed_pages_and_does_not_inspect_raw_drafts(tmp_path):
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.views import published_scope
    from ultrakb.lint import run_structural_lint
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "lint"
    initialize_kb(root)
    bound = published_scope(root).knowledge
    store = KnowledgeStore(root, bound.identity)
    store.publish(
        store.prepare_changes(
            bound,
            {
                "concepts/committed.md": b"[[concepts/COMMITTED_GHOST]]",
            },
        )
    )
    draft = root / "wiki/concepts/draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("[[concepts/UNCOMMITTED_GHOST]]")
    (root / "raw/unadmitted.md").write_text("An input outside the publication.")
    report = run_structural_lint(root)
    assert "COMMITTED_GHOST" in report
    assert "UNCOMMITTED_GHOST" not in report
    assert "unadmitted.md" not in report


def lint_knowledge(tmp_path):
    root, identity, unit = compile_input(tmp_path)
    store = KnowledgeStore(root, identity)
    store.publish(
        store.prepare_revision(
            {
                "index.md": b"# Index\n\n[[concepts/topic]]\n",
                "concepts/topic.md": b"# Topic\n\nAn unresolved [[missing]] link.\n",
            },
            expected=store.bind().publication_id,
            unit_revisions=(unit.unit_revision_id,),
            page_inputs={"concepts/topic.md": (unit.unit_revision_id,)},
        )
    )
    return root, identity


@pytest.mark.asyncio
async def test_link_repair_and_report_publish_together_without_model_configuration(tmp_path):
    root, identity = lint_knowledge(tmp_path)
    (root / ".ultrakb/config.yaml").unlink()
    scope = storage_scope(root, identity)
    version, preview = preview_link_repair(root, scope=scope)
    assert "missing" in preview
    result = await check_knowledge(
        root, LintOptions(fix=True, semantic=False, version=version), scope=scope
    )
    assert result.status == "completed", result
    assert result.files_changed == result.ghosts_removed == 1
    current = storage_scope(root, identity)
    assert current.publication_id != scope.publication_id
    assert current.knowledge.read("concepts/topic.md") == (
        b"# Topic\n\nAn unresolved missing link.\n"
    )
    assert b"[[missing]]" in scope.knowledge.read("concepts/topic.md")
    report = Path(result.report_path).relative_to(root / "wiki").as_posix()
    assert b"## Structural" in current.knowledge.read(report)
    assert (
        current.knowledge.page_input_revisions()["concepts/topic.md"]
        == (scope.knowledge.page_input_revisions()["concepts/topic.md"])
    )
    assert not (root / ".ultrakb/hashes.json").exists()
    assert get_kb_status(root, scope=current)["last_lint_availability"] == "known"
    assert get_kb_status(root, scope=scope)["last_lint_availability"] == "unrecorded"


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["publication", "report", "accepted-report"])
async def test_paid_lint_report_survives_publication_failure_without_resending(
    tmp_path, monkeypatch, interruption
):
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity = lint_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()

    def respond(request, index):
        if index == 0:
            return {
                "tool_calls": [
                    {
                        "id": "read-topic",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"concepts/topic.md"}',
                        },
                    }
                ]
            }
        return {"content": "No semantic issues in the repaired topic."}

    def unavailable(*args, **kwargs):
        raise OSError("Publication storage is unavailable")

    save = CompileWork.save

    def interrupted(self, key, *args, **kwargs):
        if key == "semantic-lint":
            if interruption == "accepted-report":
                save(self, key, *args, **kwargs)
            raise OSError("Semantic report checkpoint interrupted")
        return save(self, key, *args, **kwargs)

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as failure:
            if interruption == "publication":
                failure.setattr(KnowledgeStore, "publish", unavailable)
            else:
                failure.setattr(CompileWork, "save", interrupted)
            failed = await check_knowledge(
                root, LintOptions(fix=True), scope=scope, context=context
            )
        assert failed.status == "failed", failed
        assert storage_scope(root, identity).publication_id == scope.publication_id
        assert len(service.requests) == 2, failed
        result = await check_knowledge(root, LintOptions(fix=True), scope=scope, context=context)
        assert result.status == "completed", result
        current = storage_scope(root, identity)
        assert b"[[missing]]" not in current.knowledge.read("concepts/topic.md")
        report = Path(result.report_path).relative_to(root / "wiki").as_posix()
        assert b"No semantic issues" in current.knowledge.read(report)
        assert any(
            message["role"] == "tool" and "unresolved missing link" in message["content"]
            for message in service.requests[1]["messages"]
        )
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_committed_lint_replay_synchronizes_the_same_publication(tmp_path, monkeypatch):
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity = lint_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()

    def unavailable(*args, **kwargs):
        raise OSError("Projection storage is unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(ProjectionStore, "sync", unavailable)
        first = await check_knowledge(
            root, LintOptions(fix=True, semantic=False), scope=scope, context=context
        )
    assert first.status == "completed" and first.projection_pending, first
    result = await check_knowledge(
        root, LintOptions(fix=True, semantic=False), scope=scope, context=context
    )
    assert result.status == "completed" and not result.projection_pending, result
    assert result.publication_id == first.publication_id
    assert storage_scope(root, identity).publication_id == first.publication_id
    assert Path(result.report_path).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [False, True])
async def test_lint_rebuilds_an_unpublished_sync_plan_without_resending(
    tmp_path, monkeypatch, semantic
):
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity = lint_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    publish = KnowledgeStore.publish

    def synchronize_base(self, prepared, **kwargs):
        ProjectionStore(root, identity).sync()
        return publish(self, prepared, **kwargs)

    with StreamTransport([{"content": "No semantic issues."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as race:
            race.setattr(KnowledgeStore, "publish", synchronize_base)
            first = await check_knowledge(
                root, LintOptions(fix=True, semantic=semantic), scope=scope, context=context
            )
        assert first.status == "conflict", first
        assert storage_scope(root, identity).publication_id == scope.publication_id
        result = await check_knowledge(
            root, LintOptions(fix=True, semantic=semantic), scope=scope, context=context
        )
        assert result.status == "completed" and not result.projection_pending, result
        assert len(service.requests) == int(semantic)


@pytest.mark.asyncio
async def test_semantic_failure_keeps_the_repair_and_reports_the_unfinished_check(tmp_path):
    from error_transport import ErrorTransport

    root, identity = lint_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    with ErrorTransport(status=400) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await check_knowledge(root, LintOptions(fix=True), scope=scope)
    assert result.status == "completed", result
    assert result.quality == ("semantic_lint_failed",)
    assert "semantic_lint" in result.unfinished
    current = storage_scope(root, identity)
    assert b"[[missing]]" not in current.knowledge.read("concepts/topic.md")
    assert "Knowledge lint failed" in result.knowledge_report
    assert len(service.requests) == 1


@pytest.mark.asyncio
async def test_lint_rejects_a_stale_preview_before_model_work(tmp_path):
    root, identity = lint_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    version, _ = preview_link_repair(root, scope=scope)
    store = KnowledgeStore(root, identity)
    store.publish(store.prepare_changes(scope.knowledge, {"reports/other.md": b"Another report"}))
    current = storage_scope(root, identity)
    result = await check_knowledge(root, LintOptions(fix=True, version=version), scope=current)
    assert result.status == "conflict" and not result.resources
    assert storage_scope(root, identity).publication_id == current.publication_id


@pytest.mark.asyncio
async def test_lint_does_not_overwrite_a_publication_that_changes_during_model_work(tmp_path):
    from ultrakb.storage.blobs import put_blob
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.maintenance import Maintenance

    root, identity = lint_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    concurrent, collections = [], []
    with storage_stage(root, identity) as db:
        put_blob(db, b"Unrelated orphan")

    def respond(request, index):
        collections.append(Maintenance(root, identity).collect(max_items=1, max_bytes=100))
        store = KnowledgeStore(root, identity)
        publication = store.publish(
            store.prepare_changes(scope.knowledge, {"reports/other.md": b"Another report"})
        )
        concurrent.append(publication.publication_id)
        return {"content": "No semantic issues."}

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await check_knowledge(root, LintOptions(fix=True), scope=scope)
    assert result.status == "conflict" and not result.resources, result
    current = storage_scope(root, identity)
    assert current.publication_id == concurrent[0]
    assert b"[[missing]]" in current.knowledge.read("concepts/topic.md")
    assert not current.knowledge.file_page(prefix="reports/", after="reports/other.md")
    assert len(service.requests) == 1
    assert collections[0].deleted_count == 1


@pytest.mark.asyncio
async def test_link_repair_keeps_the_original_image_reference_and_bytes(tmp_path):
    root, identity = lint_knowledge(tmp_path)
    store = KnowledgeStore(root, identity)
    store.publish(
        store.prepare_changes(
            store.bind(),
            {
                "concepts/topic.md": b"# Topic\n\n![red](../media/chart.svg)\n\n[[missing]]\n",
                "media/chart.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><text>red</text></svg>',
            },
        )
    )
    scope = storage_scope(root, identity)
    result = await check_knowledge(root, LintOptions(fix=True, semantic=False), scope=scope)
    assert result.status == "completed", result
    current = storage_scope(root, identity)
    assert current.knowledge.read("concepts/topic.md") == (
        b"# Topic\n\n![red](../media/chart.svg)\n\nmissing\n"
    )
    assert current.knowledge.read("media/chart.svg") == scope.knowledge.read("media/chart.svg")


@pytest.mark.asyncio
async def test_explicit_lint_credentials_choose_the_actual_provider(tmp_path):
    from ultrakb.config import LlmCredentialBundle

    root, identity = lint_knowledge(tmp_path)
    with (
        StreamTransport([{"content": "Wrong provider."}]) as configured,
        StreamTransport([{"content": "Chosen provider."}]) as selected,
    ):
        (root / ".env").write_text(f"LLM_API_KEY=first\nOPENAI_API_BASE={configured.url}\n")
        result = await check_knowledge(
            root,
            scope=storage_scope(root, identity),
            bundle=LlmCredentialBundle(api_key="second", base_url=selected.url),
        )
        assert result.status == "completed", result
        assert result.knowledge_report == "Chosen provider."
        assert len(selected.requests) == 1 and not configured.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["stop", "write"])
@pytest.mark.parametrize("tool_call", [False, True])
async def test_received_sdk_response_is_recovered_before_accepted_step(
    tmp_path, monkeypatch, interruption, tool_call
):
    from ultrakb.locks import LockCancelled
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity = lint_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    save = CompileWork.save

    def interrupted(self, key, *args, **kwargs):
        if key == "maintenance-agent:0":
            if interruption == "stop":
                raise LockCancelled("Stopped before SDK response acceptance")
            raise OSError("SDK response checkpoint write unavailable")
        return save(self, key, *args, **kwargs)

    def respond(request, index):
        if tool_call and index == 0:
            return {
                "tool_calls": [
                    {
                        "id": "read-topic",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"concepts/topic.md"}',
                        },
                    }
                ]
            }
        return {"content": "Retain the received report."}

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            fault.setattr(CompileWork, "save", interrupted)
            if interruption == "stop":
                with pytest.raises(LockCancelled):
                    await check_knowledge(root, scope=scope, context=context)
            else:
                first = await check_knowledge(root, scope=scope, context=context)
                assert first.status == "blocked", first
        assert len(service.requests) == 1
        assert storage_scope(root, identity).publication_id == scope.publication_id
        result = await check_knowledge(root, scope=scope, context=context)
        assert result.status == "completed", result
        assert result.knowledge_report == "Retain the received report."
        assert len(service.requests) == 1 + int(tool_call)
        if tool_call:
            assert any(
                message["role"] == "tool" and "[[missing]]" in message["content"]
                for message in service.requests[1]["messages"]
            )


@pytest.mark.asyncio
async def test_received_report_without_usage_keeps_its_text_when_acceptance_resumes(
    tmp_path, monkeypatch
):
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity = lint_knowledge(tmp_path)
    scope, context = storage_scope(root, identity), ExecutionContext()
    save = CompileWork.save

    def interrupted(self, key, *args, **kwargs):
        if key == "maintenance-agent:0":
            raise OSError("SDK response checkpoint write unavailable")
        return save(self, key, *args, **kwargs)

    with StreamTransport([{"content": "Keep this exact report."}], usage_events=[]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        with monkeypatch.context() as fault:
            fault.setattr(CompileWork, "save", interrupted)
            first = await check_knowledge(root, scope=scope, context=context)
        assert first.status == "blocked", first
        result = await check_knowledge(root, scope=scope, context=context)
        assert result.status == "completed" and not result.quality, result
        assert result.knowledge_report == "Keep this exact report."
        assert len(service.requests) == 1


@pytest.mark.parametrize("adapter", ["cli", "http", "native"])
def test_each_adapter_reports_the_committed_lint_when_projection_is_pending(
    tmp_path, monkeypatch, adapter
):
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity = lint_knowledge(tmp_path)
    scope = storage_scope(root, identity)

    def unavailable(*args, **kwargs):
        raise OSError("Projection storage is unavailable")

    monkeypatch.setattr(ProjectionStore, "sync", unavailable)
    with StreamTransport([{"content": "No semantic issues."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        if adapter == "cli":
            from click.testing import CliRunner

            import ultrakb.cli as cli

            monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
            monkeypatch.setattr(cli, "_selected_scope", lambda *args, **kwargs: scope)
            result = CliRunner().invoke(cli.cli, ["lint", "--fix"])
            assert result.exit_code == 0, result.output
            assert "synchronization pending" in result.output
        elif adapter == "http":
            from fastapi.testclient import TestClient

            import ultrakb.api as api
            import ultrakb.api_views as api_views

            monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
            monkeypatch.setattr(api, "_resolve_kb", lambda *args: root)
            monkeypatch.setattr(api_views, "view_scope", lambda *args, **kwargs: scope)
            with TestClient(api.create_app()) as client:
                response = client.post(
                    "/api/v1/lint", json={"kb": str(root), "view_id": "legacy", "fix": True}
                )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["projection_pending"]
            assert result["publication_id"] == storage_scope(root, identity).publication_id
            assert "synchronize wiki" in result["unfinished"]
        else:
            import json
            import subprocess
            import sys

            version, _ = preview_link_repair(root, scope=scope)
            script = tmp_path / "lint_worker.py"
            script.write_text(
                """import json, multiprocessing, sys
from pathlib import Path
from uuid import uuid4

def run(root, version, queue):
    from ultrakb.application.execution import ExecutionContext
    import ultrakb.application.views as views
    from ultrakb.storage.database import open_database
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import CheckKnowledge
    from ultrakb.runtime.worker import _execute
    from ultrakb.storage.wiki_projection import ProjectionStore
    root = Path(root)
    with open_database(root, read_only=True) as db:
        identity = db.identity
    scope = views.storage_scope(root, identity)
    views.view_scope = lambda *args, **kwargs: scope
    def unavailable(*args, **kwargs):
        raise OSError("Projection storage is unavailable")
    ProjectionStore.sync = unavailable
    request = CheckKnowledge(fix=True, version=version, view_id="legacy")
    worker = UnitIdentity.create(uuid4().hex, 0, str(root), request, generation=identity.generation)
    result = _execute(request, worker, ExecutionContext())
    queue.put({"status": result.status, "revision": result.revision,
               "unfinished": result.unfinished, "error": result.error})

if __name__ == "__main__":
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=run, args=(sys.argv[1], sys.argv[2], queue))
    child.start()
    result = queue.get(timeout=30)
    child.join(timeout=5)
    assert child.exitcode == 0
    print(json.dumps(result))
"""
            )
            completed = subprocess.run(
                [sys.executable, str(script), str(root), version],
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert completed.returncode == 0, completed.stderr[-2000:]
            result = json.loads(completed.stdout.splitlines()[-1])
            assert result["status"] == "completed", result
            assert result["revision"] == storage_scope(root, identity).publication_id
            assert "synchronize wiki" in result["unfinished"]
        assert len(service.requests) == 1
    current = storage_scope(root, identity)
    assert b"[[missing]]" not in current.knowledge.read("concepts/topic.md")
    assert len(current.knowledge.file_page(prefix="reports/")) == 1
