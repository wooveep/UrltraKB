"""Recovered HTML with no readable body must not become a document."""

from zipfile import ZipFile

import pytest

pytest_plugins = ("pending_fixtures",)


def test_script_only_embedded_html_is_not_imported(kb_dir, writer_document, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending
    from openkb.source_catalog import list_sources

    with ZipFile(writer_document, "a") as package:
        package.writestr(
            "word/embeddings/api.htm",
            '<html><head><title>API guide</title></head><body><div id="app"></div>'
            '<script>var projectJSON = {"apis": [{"name": "Login"}]};</script></body></html>',
        )
    parent = import_document(kb_dir, writer_document)
    result = process_pending(kb_dir)
    assert [source.source_id for source in list_sources(kb_dir)] == [parent.source_id]
    job = next(job for job in result["jobs"] if job["kind"] == "import")
    assert job["status"] == "not_imported"
    assert job["source_id"] is None and job["source_revision_id"] is None
    assert "no readable body" in job["message"]
    assert job["result"] is None
    assert result["runnable"] == result["imports_pending"] == 0
    assert job["model_usage"]["current"]["requests"] == 0
    assert process_pending(kb_dir)["processed"] == 0
    assert pending_status(kb_dir)["jobs"] == result["jobs"]

    from openkb.application.ingestion import pending_counts
    from openkb.application.pending import cancel_execution_group, retry_pending_job
    from openkb.source_catalog import read_admission, read_source

    admission = read_admission(kb_dir, read_source(kb_dir, parent.source_id))
    assert pending_counts(kb_dir, admission)["imports_pending"] == 0
    with pytest.raises(ValueError, match="Only failed"):
        retry_pending_job(kb_dir, job["id"])
    cancel_execution_group(kb_dir, job["root_import_id"])
    assert process_pending(kb_dir)["processed"] == 0
    assert (
        next(j for j in pending_status(kb_dir)["jobs"] if j["id"] == job["id"])["status"]
        == "not_imported"
    )


@pytest.mark.parametrize("body", ["", "<!-- no body -->", '<script>var data = "API";</script>'])
def test_empty_html_direct_import_has_no_document_artifacts(kb_dir, tmp_path, pdf_model, body):
    from openkb.application.documents import import_document
    from openkb.source_catalog import list_sources

    path = tmp_path / "empty.html"
    path.write_text("<html><head><title>Guide</title></head><body>" + body + "</body></html>")
    result = import_document(kb_dir, path)
    assert result.status == "skipped"
    assert result.source_id is None and result.source_revision_id is None
    assert result.units == () and result.unfinished == ()
    assert result.quality == ("empty_html_not_imported",)
    assert not list_sources(kb_dir)
    assert not list((kb_dir / ".openkb/catalog/source-revisions").glob("*.json"))
    assert not list((kb_dir / ".openkb/catalog/units").glob("*.json"))
    assert not list((kb_dir / ".openkb/knowledge").glob("*"))
    assert result.model_usage["current"]["requests"] == 0


def test_embedded_html_with_static_body_still_imports(kb_dir, writer_document, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.pending import process_pending
    from openkb.documents import read_document_source
    from openkb.source_catalog import list_sources

    with ZipFile(writer_document, "a") as package:
        package.writestr(
            "word/embeddings/api.htm",
            "<html><body><p>Login requires user and pwd.</p>"
            '<script>var data = "dynamic content";</script></body></html>',
        )
    parent = import_document(kb_dir, writer_document)
    result = process_pending(kb_dir)
    child = next(source for source in list_sources(kb_dir) if source.source_id != parent.source_id)
    assert read_document_source(kb_dir, child.source_id)["content"].strip() == (
        "Login requires user and pwd."
    )
    assert next(job for job in result["jobs"] if job["kind"] == "import")["status"] == "completed"


def test_runtime_reports_not_imported_as_skipped(kb_dir, writer_document, pdf_model, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.pending import claim_pending_job, process_pending, run_pending_job
    from openkb.runtime.records import UnitIdentity
    from openkb.runtime.requests import RunPendingJob
    from openkb.runtime.worker import _execute
    from openkb.source_catalog import list_sources

    with ZipFile(writer_document, "a") as package:
        package.writestr("word/embeddings/empty.htm", "<html><body></body></html>")
    parent = import_document(kb_dir, writer_document)
    process_pending(kb_dir, max_jobs=1)
    job = claim_pending_job(kb_dir)
    outcome = run_pending_job(kb_dir, job["id"], job["dispatch_id"])
    # Replay the real storage-boundary result through the worker's status projection.
    monkeypatch.setattr("openkb.application.pending.run_pending_job", lambda *a, **k: outcome)
    result = _execute(
        RunPendingJob(job["id"], job["dispatch_id"]),
        UnitIdentity("a" * 32, "1", str(kb_dir), "b" * 64),
        ExecutionContext(),
    )
    assert result.status == "skipped" and "no readable body" in result.error, result
    assert result.model_usage["current"]["requests"] == 0
    assert [source.source_id for source in list_sources(kb_dir)] == [parent.source_id]
