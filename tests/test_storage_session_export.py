"""Explicit transcripts publish from retained originals, independent of legacy files."""

from pathlib import Path

import pytest
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.conversations import continue_conversation, read_conversation
from ultrakb.application.pages import read_page
from ultrakb.application.sessions import export_conversation
from ultrakb.application.views import storage_scope

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_export_retains_originals_and_uniquifies_without_another_model_send(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    with StreamTransport([{"content": "The answer is 17. See [[index]]."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        answer = await continue_conversation(
            root,
            "What is the original value?",
            new_session_id="retained-export",
            scope=storage_scope(root, identity),
        )
        assert answer.status == "completed", answer.error
        history = read_conversation(root, answer.session_id, scope=storage_scope(root, identity))
        first = export_conversation(
            root, answer.session_id, name="Original transcript", scope=storage_scope(root, identity)
        )
        second = export_conversation(
            root, answer.session_id, name="Original transcript", scope=storage_scope(root, identity)
        )
    assert first.status == second.status == "exported"
    assert first.resources != second.resources
    scope = storage_scope(root, identity)
    for result in (first, second):
        relative = Path(result.resources[0]).relative_to(root / "wiki").as_posix()
        page = read_page(root, relative, scope=scope)
        assert "What is the original value?" in page.content
        assert "The answer is 17. See [[index]]." in page.content
        assert 'session: "retained-export"' in page.content
    assert read_conversation(root, answer.session_id, scope=scope).version == history.version
    assert len(service.requests) == 1
    assert not (root / ".ultrakb/sessions").exists()


def test_export_includes_all_original_pages_and_pending_questions_once(tmp_path):
    from ultrakb.storage.execution_store import ExecutionStore
    from ultrakb.storage.session_store import SessionStore

    root, identity, publication = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("long-export", model="gpt-4o-mini", language="en")
    for ordinal in range(129):
        store.append_exchange(
            "long-export",
            f"turn-{ordinal}",
            f"QUESTION_{ordinal}.",
            "Answer.",
            system="SYSTEM_INSTRUCTION_NOT_A_TITLE" if ordinal == 0 else None,
        )
    executions = ExecutionStore(root, identity)
    for ordinal in range(130):
        execution = executions.begin("pending-export", fingerprint="fixture", max_calls=0)
        store.start_submission(
            "long-export",
            f"pending-{ordinal}",
            f"UNFINISHED_{ordinal}.",
            execution_id=execution.execution_id,
            publication_id=publication.publication_id,
            order=ordinal,
        )
        executions.finish(execution.execution_id, state="failed", work_settled=True)
    result = export_conversation(root, "long-export", scope=storage_scope(root, identity))
    assert result.status == "exported"
    assert Path(result.resources[0]).name.startswith("question-0-")
    path = Path(result.resources[0]).relative_to(root / "wiki").as_posix()
    content = read_page(root, path, scope=storage_scope(root, identity)).content
    for ordinal in range(129):
        assert content.count(f"QUESTION_{ordinal}.") == 1
    for ordinal in range(130):
        assert content.count(f"UNFINISHED_{ordinal}.") == 1
    assert content.index("QUESTION_0.") < content.index("QUESTION_128.")
    assert content.index("UNFINISHED_0.") < content.index("UNFINISHED_129.")


def test_export_reports_master_success_while_the_worktree_is_paused_and_keeps_stale_candidates(
    tmp_path,
):
    from ultrakb.application.wiki_worktree import begin_external_edit
    from ultrakb.storage.session_store import SessionStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("paused-export", model="gpt-4o-mini", language="en", title="Paused")
    store.append_exchange("paused-export", "turn", "Question.", "Retained answer.")
    ProjectionStore(root, identity).sync()
    begin_external_edit(root, identity)
    before = storage_scope(root, identity)
    result = export_conversation(root, "paused-export", scope=before)
    assert result.status == "exported"
    assert result.projection.status == "paused"
    assert result.unfinished == ("synchronize wiki",)
    path = Path(result.resources[0]).relative_to(root / "wiki").as_posix()
    assert "Retained answer." in read_page(root, path, scope=storage_scope(root, identity)).content
    assert not Path(result.resources[0]).exists()
    stale = export_conversation(root, "paused-export", scope=before)
    assert stale.status == "conflict" and stale.candidate_revision_id
    assert stale.resources == () and stale.publication_id is None
    assert storage_scope(root, identity).publication_id == result.publication_id


def test_export_of_a_deleted_conversation_is_missing_and_cannot_revive_it(tmp_path):
    from ultrakb.application.sessions import delete_conversation
    from ultrakb.storage.session_store import SessionStore

    root, identity, publication = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("deleted-export", model="gpt-4o-mini", language="en")
    store.append_exchange("deleted-export", "turn", "Question.", "Answer.")
    scope = storage_scope(root, identity)
    assert delete_conversation(root, "deleted-export", scope=scope).status == "deleted"
    result = export_conversation(root, "deleted-export", scope=scope)
    assert result.status == "missing" and result.resources == ()
    assert storage_scope(root, identity).publication_id == publication.publication_id


def test_native_worker_keeps_the_committed_export_and_pending_projection(tmp_path, monkeypatch):
    from ultrakb.application import views
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.wiki_worktree import begin_external_edit
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import ExportConversation
    from ultrakb.runtime.worker import _execute
    from ultrakb.storage.session_store import SessionStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("worker-export", model="gpt-4o-mini", language="en")
    store.append_exchange("worker-export", "turn", "Question.", "Answer.")
    ProjectionStore(root, identity).sync()
    begin_external_edit(root, identity)
    monkeypatch.setattr(views, "view_scope", lambda root, view_id: storage_scope(root, identity))
    request = ExportConversation("worker-export", view_id="legacy")
    token = UnitIdentity.create("a" * 32, 0, str(root), request, generation=identity.generation)
    result = _execute(request, token, ExecutionContext(storage_identity=identity))
    assert result.status == "completed"
    assert result.revision == storage_scope(root, identity).publication_id
    assert result.unfinished == ("synchronize wiki",)
    assert result.context["projection"]["status"] == "paused"
