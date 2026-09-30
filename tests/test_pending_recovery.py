"""Job claims, unknown work and committed business facts survive delivery failures."""

import pytest

pytest_plugins = ("pending_fixtures",)


def test_dispatched_without_started_can_resume_and_old_delivery_is_ignored(
    kb_dir, embedded_docx, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.application.pending import claim_pending_job, pending_status, run_pending_job

    import_document(kb_dir, embedded_docx)
    old = claim_pending_job(kb_dir)
    fresh = claim_pending_job(kb_dir)
    assert old["id"] == fresh["id"] and old["dispatch_id"] != fresh["dispatch_id"]
    assert run_pending_job(kb_dir, old["id"], old["dispatch_id"])["status"] == "skipped"
    assert run_pending_job(kb_dir, fresh["id"], fresh["dispatch_id"])["status"] == "completed"
    assert pending_status(kb_dir)["groups"][0]["sources"] == 2


def test_started_unknown_import_requires_explicit_retry(
    kb_dir, embedded_docx, office_runtime, pdf_model, monkeypatch
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending, retry_pending_job

    import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    completion = litellm.completion

    def worker_died(**kwargs):
        raise SystemExit("Fixture worker disappeared before model result")

    monkeypatch.setattr(litellm, "completion", worker_died)
    with pytest.raises(SystemExit):
        process_pending(kb_dir, max_jobs=1)
    monkeypatch.setattr(litellm, "completion", completion)
    process_pending(kb_dir)
    job = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert job["status"] == "interrupted"
    retry_pending_job(kb_dir, job["id"])
    process_pending(kb_dir)
    assert (
        next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")["status"]
        == "completed"
    )


def test_business_commit_wins_when_job_completion_record_is_lost(
    kb_dir, embedded_docx, office_runtime, pdf_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending
    from openkb.mutation import MutationSnapshot
    from openkb.pending.records import ImportIntent
    from openkb.source_catalog import read_record

    import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    job = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    commit = MutationSnapshot.mark_committed
    lost = False

    def lose_completion(snapshot):
        nonlocal lost
        saved = read_record(kb_dir, "import-intents", job["id"], ImportIntent)
        if not lost and snapshot.operation == "update-pending-job" and saved.status == "completed":
            lost = True
            raise OSError("Fixture lost job result marker after knowledge committed")
        commit(snapshot)

    monkeypatch.setattr(MutationSnapshot, "mark_committed", lose_completion)
    result = process_pending(kb_dir)
    assert lost and all(outcome["status"] == "completed" for outcome in result["outcomes"])
    assert process_pending(kb_dir)["processed"] == 0


def test_cli_and_api_expose_waiting_counts_without_restarting_them(
    kb_dir, embedded_docx, pdf_model
):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, update_execution_budget
    from openkb.cli import cli
    from openkb.config import register_kb

    import_document(kb_dir, embedded_docx)
    group = pending_status(kb_dir)["groups"][0]
    update_execution_budget(kb_dir, group["root_import_id"], {"max_sources": 1})
    command = CliRunner().invoke(cli, ["process-pending", "--kb", str(kb_dir)])
    assert command.exit_code == 0, command.output
    assert json.loads(command.output)["discovery_pending"] == 1
    register_kb(kb_dir)
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/pending", params={"kb": kb_dir.name})
        assert response.status_code == 200, response.text
        assert response.json()["runnable"] == 0


def test_group_cancel_after_lost_receipt_preserves_committed_import_status(
    kb_dir, embedded_docx, office_runtime, pdf_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.application.pending import cancel_execution_group, pending_status, process_pending
    from openkb.documents import read_document_source
    from openkb.mutation import MutationSnapshot
    from openkb.pending.records import ImportIntent
    from openkb.source_catalog import read_record

    import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    job = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    commit = MutationSnapshot.mark_committed
    lost = False

    def lose_completion(snapshot):
        nonlocal lost
        saved = read_record(kb_dir, "import-intents", job["id"], ImportIntent)
        if not lost and snapshot.operation == "update-pending-job" and saved.status == "completed":
            lost = True
            raise SystemExit("Worker lost after the business publication")
        commit(snapshot)

    monkeypatch.setattr(MutationSnapshot, "mark_committed", lose_completion)
    with pytest.raises(SystemExit):
        process_pending(kb_dir, max_jobs=1)
    monkeypatch.setattr(MutationSnapshot, "mark_committed", commit)
    interrupted = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert read_document_source(kb_dir, interrupted["source_id"])["status"] == "completed"
    cancel_execution_group(kb_dir, interrupted["root_import_id"])
    process_pending(kb_dir)
    after = next(job for job in pending_status(kb_dir)["jobs"] if job["id"] == interrupted["id"])
    assert after["status"] == "completed"


def test_bad_candidate_does_not_hide_a_later_complete_file(kb_dir, embedded_docx, pdf_model):
    import io
    from zipfile import ZipFile

    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending

    broken = io.BytesIO()
    with ZipFile(broken, "w") as nested:
        nested.writestr("[Content_Types].xml", "<malformed")
        nested.writestr("word/document.xml", "<document/>")
    with ZipFile(embedded_docx, "a") as outer:
        outer.writestr("word/embeddings/A_bad.docx", broken.getvalue())
    import_document(kb_dir, embedded_docx)
    result = process_pending(kb_dir, max_jobs=1)
    imports = [j for j in pending_status(kb_dir)["jobs"] if j["kind"] == "import"]
    assert result["outcomes"][0]["status"] == "completed"
    assert [job["filename"] for job in imports] == ["Recovered.docx"]


def test_foreign_source_reference_cannot_complete_unimported_job(kb_dir, embedded_docx, pdf_model):
    import json

    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending

    import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    job = next(j for j in pending_status(kb_dir)["jobs"] if j["kind"] == "import")
    foreign = embedded_docx.with_name("unrelated.txt")
    foreign.write_text("A separate ordinary document.", encoding="utf-8")
    other = import_document(kb_dir, foreign)
    assert other.status == "added"
    path = kb_dir / ".openkb/catalog/import-intents" / (job["id"] + ".json")
    value = json.loads(path.read_text())
    value["source_id"] = other.source_id
    value["source_revision_id"] = other.source_revision_id
    path.write_text(json.dumps(value))
    try:
        process_pending(kb_dir)
    except ValueError:
        return
    saved = next(j for j in pending_status(kb_dir)["jobs"] if j["id"] == job["id"])
    assert saved["status"] != "completed", (
        "Unrelated publication was accepted as this job business result"
    )
