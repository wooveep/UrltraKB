"""Human edits and explicit acceptance use the same durable knowledge boundary."""

from types import SimpleNamespace

import pytest

from ultrakb.application.pages import read_page, save_page


def test_proposal_remains_reviewable_and_can_be_accepted_after_import_cleanup(
    kb_dir, pending_proposal
):
    from ultrakb.application.proposals import accept_proposal, read_proposal

    opened = read_proposal(kb_dir, pending_proposal)
    assert "A reviewed human annotation." in opened.diff
    assert "+" in opened.diff and "17" in opened.diff
    accepted = accept_proposal(kb_dir, pending_proposal, version=opened.version)
    assert accepted.status == "added"
    assert "17" in read_page(kb_dir, "summaries/fixture").body
    assert accept_proposal(kb_dir, pending_proposal, version=opened.version).status == "skipped"


def test_editing_after_review_keeps_the_proposal_and_current_page(kb_dir, pending_proposal):
    from ultrakb.application.proposals import accept_proposal, read_proposal

    opened = read_proposal(kb_dir, pending_proposal)
    page = read_page(kb_dir, "summaries/fixture")
    save_page(kb_dir, page.path, "New human explanation.", version=page.version)
    assert accept_proposal(kb_dir, pending_proposal, version=opened.version).status == "blocked"
    assert read_page(kb_dir, page.path).body == "New human explanation."
    assert read_proposal(kb_dir, pending_proposal).status == "superseded"


def test_proposals_are_confined_to_the_knowledge_base(
    kb_dir, pending_proposal, tmp_path, proposal_case
):
    from test_storage_questions import question_knowledge

    from ultrakb.application.proposals import accept_proposal, list_proposals, read_proposal
    from ultrakb.knowledge_scope import live_scope

    question_knowledge(tmp_path / "foreign")
    foreign = live_scope(tmp_path / "foreign" / "kb")
    opened = read_proposal(kb_dir, pending_proposal)
    with pytest.raises(ValueError, match="different knowledge base"):
        list_proposals(kb_dir, scope=foreign)
    with pytest.raises(ValueError, match="different knowledge base"):
        read_proposal(kb_dir, pending_proposal, scope=foreign)
    with pytest.raises(ValueError, match="different knowledge base"):
        accept_proposal(kb_dir, pending_proposal, version=opened.version, scope=foreign)
    assert (
        read_page(kb_dir, "summaries/fixture").body.strip().endswith("A reviewed human annotation.")
    )


def test_recompile_preserves_manual_changes_as_a_new_proposal(
    kb_dir, pending_proposal, monkeypatch
):
    import asyncio

    from ultrakb.application.proposals import accept_proposal, list_proposals, read_proposal
    from ultrakb.application.recompilation import recompile_document

    proposal = read_proposal(kb_dir, pending_proposal)
    accepted = accept_proposal(kb_dir, pending_proposal, version=proposal.version)
    page = read_page(kb_dir, "summaries/fixture")
    save_page(kb_dir, page.path, "Edited by the user.", version=page.version)
    from stream_transport import StreamTransport
    from test_storage_import_proposals import compiler_responses

    with StreamTransport(compiler_responses()[:2]) as service:
        (kb_dir / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = asyncio.run(
            recompile_document(kb_dir, accepted.source_id, unit_id=proposal.unit_id)
        )
    assert result.status == "blocked"
    assert read_page(kb_dir, page.path).body == "Edited by the user."
    assert len(list_proposals(kb_dir)) == 1, result


def test_another_source_cannot_restore_a_manually_deleted_shared_page():
    from ultrakb.storage.generated_baselines import protected_knowledge_pages

    # A baseline survives the manual deletion. An incoming candidate from a
    # different source must still be reviewed before recreating the shared page.
    generated_baselines = {"concepts/fact.md": "original-generated-hash"}
    current_pages = {}
    incoming_pages = {"concepts/fact.md": "new-source-generated-hash"}
    assert protected_knowledge_pages(current_pages, incoming_pages, generated_baselines) == {
        "concepts/fact.md"
    }
    assert (
        protected_knowledge_pages(
            current_pages, {"concepts/another.md": "new"}, generated_baselines
        )
        == set()
    )
    assert (
        protected_knowledge_pages(generated_baselines, incoming_pages, generated_baselines) == set()
    )


@pytest.mark.parametrize("entrypoint", ["cli", "api", "worker"])
def test_acceptance_entrypoints_use_the_saved_review(
    kb_dir, pending_proposal, monkeypatch, entrypoint
):
    from ultrakb.application.proposals import read_proposal

    opened = read_proposal(kb_dir, pending_proposal)
    if entrypoint == "cli":
        from click.testing import CliRunner

        from ultrakb.cli import cli

        runner = CliRunner()
        shown = runner.invoke(cli, ["--kb-dir", str(kb_dir), "proposals", "show", pending_proposal])
        assert opened.version in shown.output
        accepted = runner.invoke(
            cli,
            [
                "--kb-dir",
                str(kb_dir),
                "proposals",
                "accept",
                pending_proposal,
                "--version",
                opened.version,
            ],
        )
        assert accepted.exit_code == 0, accepted.output
    elif entrypoint == "api":
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app

        monkeypatch.setenv("ULTRAKB_API_TOKEN", "secret")
        monkeypatch.setattr("ultrakb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/proposal/accept",
                json={
                    "kb": "test",
                    "proposal_id": pending_proposal,
                    "version": opened.version,
                },
                headers={"Authorization": "Bearer secret"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "added"
    else:
        from ultrakb.application.execution import ExecutionContext
        from ultrakb.runtime.requests import AcceptProposal
        from ultrakb.runtime.worker import _execute

        result = _execute(
            AcceptProposal(pending_proposal, opened.version),
            SimpleNamespace(kb_dir=str(kb_dir)),
            ExecutionContext(),
        )
        assert result.status == "completed"
    body = read_page(kb_dir, "summaries/fixture").body
    assert "17" in body and "value" in body.lower()


def test_desktop_loads_the_persisted_difference(kb_dir, pending_proposal):
    import os
    import subprocess
    import sys

    pytest.importorskip("PySide6")
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys, time
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMainWindow
from ultrakb.desktop.io import LocalIO
from ultrakb.desktop.proposals import ProposalsDialog

app = QApplication([])
window = QMainWindow()
from ultrakb.application.views import published_scope
window.scope = published_scope(Path(sys.argv[1]))
window.io = LocalIO()
view = ProposalsDialog(window, Path(sys.argv[1]))
view.show()
deadline = time.monotonic() + 10
while not view.list.count() and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert view.list.count() == 1, view.status.text()
view.list.setCurrentRow(0)
while not view.accept_button.isEnabled() and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert 'A reviewed human annotation.' in view.diff.toPlainText()
assert view.accept_button.isEnabled()
view.close()
window.io.stop()
while not window.io.stopped() and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert window.io.stopped()
""",
            str(kb_dir),
        ],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=True,
        timeout=20,
    )


@pytest.fixture
def proposal_case(tmp_path):
    import asyncio

    from stream_transport import StreamTransport
    from test_storage_import_proposals import compiler_responses, reviewed_import

    with StreamTransport(compiler_responses()) as service:
        case = asyncio.run(reviewed_import(tmp_path, service))
        yield case
        assert len(service.requests) == 4


@pytest.fixture
def kb_dir(proposal_case):
    return proposal_case[0]


@pytest.fixture
def pending_proposal(proposal_case):
    return proposal_case[-1].proposal_id


def test_manual_edit_keeps_the_original_publication_and_generated_baseline(kb_dir):
    from ultrakb.application.views import published_scope

    before = published_scope(kb_dir).knowledge
    baselines = before.generated_baselines()
    body = before.read("summaries/fixture.md")
    page = read_page(kb_dir, "summaries/fixture")
    saved = save_page(kb_dir, page.path, "Another human explanation.", version=page.version)
    assert saved.status == "saved"
    after = published_scope(kb_dir).knowledge
    assert before.read("summaries/fixture.md") == body
    assert read_page(kb_dir, "summaries/fixture").body.strip() == "Another human explanation."
    assert after.generated_baselines() == baselines


def test_native_candidate_keeps_paid_work_and_original_after_external_input_disappears(
    proposal_case,
):
    from native_catalog_fixtures import original_bytes

    root, _, unit, _, context, pending, proposal = proposal_case
    assert pending.status == "blocked"
    assert context.executor.calls == 2
    assert original_bytes(root, unit.source_revision_id) == b"The value is 17."
    assert "A reviewed human annotation." in proposal.diff
    assert "+" in proposal.diff and "17" in proposal.diff


def test_acceptance_resumes_a_cancelled_attempt(kb_dir, pending_proposal):
    from threading import Event

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.proposals import accept_proposal, read_proposal

    stopped = Event()

    def stop_at_acceptance(event):
        data = event.get("data", {})
        if data.get("stage") == "import.review.accept.v1" and data.get("status") == "started":
            stopped.set()

    review = read_proposal(kb_dir, pending_proposal)
    first = accept_proposal(
        kb_dir,
        pending_proposal,
        version=review.version,
        context=ExecutionContext(cancelled=stopped.is_set, on_event=stop_at_acceptance),
    )
    assert stopped.is_set() and first.status == "stopped", first
    accepted = accept_proposal(kb_dir, pending_proposal, version=review.version)
    assert accepted.status == "added", accepted
