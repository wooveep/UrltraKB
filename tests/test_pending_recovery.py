"""Job claims, unknown work and committed business facts survive delivery failures."""

import pytest

pytest_plugins = ("pending_fixtures",)


def test_dispatched_without_started_can_resume_and_old_delivery_is_ignored(
    kb_dir, embedded_docx, pdf_model
):
    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import claim_pending_job, pending_status, run_pending_job

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

    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import pending_status, process_pending, retry_pending_job

    import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    completion = litellm.completion

    def worker_died(**kwargs):
        completion(**kwargs)
        raise SystemExit("Worker disappeared before delivering the genuine model result")

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


def test_cli_and_api_expose_waiting_counts_without_restarting_them(
    kb_dir, embedded_docx, pdf_model
):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from ultrakb.api import create_app
    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import pending_status, update_execution_budget
    from ultrakb.cli import cli
    from ultrakb.config import register_kb

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


def test_bad_candidate_does_not_hide_a_later_complete_file(kb_dir, embedded_docx, pdf_model):
    import io
    from zipfile import ZipFile

    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import pending_status, process_pending

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
