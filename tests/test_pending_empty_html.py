"""Recovered HTML with no readable body must not become a document."""

import json
from zipfile import ZipFile

import pytest

pytest_plugins = ("pending_fixtures", "processing_fixtures")
pytestmark = pytest.mark.integration


def test_script_only_embedded_html_is_not_imported(kb_dir, writer_document, model_context):
    from native_catalog_fixtures import list_sources

    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import pending_status, process_pending, read_pending_job

    with ZipFile(writer_document, "a") as package:
        package.writestr(
            "word/embeddings/api.htm",
            '<html><head><title>API guide</title></head><body><div id="app"></div>'
            '<script>var projectJSON = {"apis": [{"name": "Login"}]};</script></body></html>',
        )
    parent = import_document(kb_dir, writer_document, context=model_context)
    result = process_pending(kb_dir, context=model_context)
    assert [source.source_id for source in list_sources(kb_dir)] == [parent.source_id]
    job = next(job for job in result["jobs"] if job.get("filename") == "api.htm")
    job = read_pending_job(kb_dir, job["id"])
    assert job["status"] == "not_imported", json.dumps(job, indent=2)
    assert job["source_id"] is None and job["source_revision_id"] is None
    assert "no readable body" in job["message"]
    assert job["result"]["status"] == "skipped"
    assert job["result"]["quality"] == ("empty_html_not_imported",)
    assert result["runnable"] == result["imports_pending"] == 0
    assert job["model_usage"]["current"]["requests"] == 0
    assert process_pending(kb_dir, context=model_context)["processed"] == 0
    assert pending_status(kb_dir)["jobs"] == result["jobs"]

    from ultrakb.application.pending import cancel_execution_group, retry_pending_job

    with pytest.raises(ValueError, match="cannot be retried"):
        retry_pending_job(kb_dir, job["id"])
    cancel_execution_group(kb_dir, job["root_import_id"])
    assert process_pending(kb_dir, context=model_context)["processed"] == 0
    assert (
        next(j for j in pending_status(kb_dir)["jobs"] if j["id"] == job["id"])["status"]
        == "not_imported"
    )


@pytest.mark.parametrize("body", ["", "<!-- no body -->", '<script>var data = "API";</script>'])
def test_empty_html_direct_import_has_no_document_artifacts(kb_dir, tmp_path, model_context, body):
    from native_catalog_fixtures import list_sources

    from ultrakb.application.documents import import_document

    path = tmp_path / "empty.html"
    path.write_text("<html><head><title>Guide</title></head><body>" + body + "</body></html>")
    result = import_document(kb_dir, path, context=model_context)
    assert result.status == "skipped"
    assert result.source_id is None and result.source_revision_id is None
    assert result.units == () and result.unfinished == ()
    assert result.quality == ("empty_html_not_imported",)
    assert not list_sources(kb_dir)
    assert result.model_usage["current"]["requests"] == 0


def test_embedded_html_with_static_body_still_imports(kb_dir, writer_document, model_context):
    from native_catalog_fixtures import list_sources

    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import process_pending
    from ultrakb.documents import read_document_source

    with ZipFile(writer_document, "a") as package:
        package.writestr(
            "word/embeddings/api.htm",
            "<html><body><p>Login requires user and pwd.</p>"
            '<script>var data = "dynamic content";</script></body></html>',
        )
    parent = import_document(kb_dir, writer_document, context=model_context)
    result = process_pending(kb_dir, context=model_context)
    child = next(source for source in list_sources(kb_dir) if source.source_id != parent.source_id)
    assert read_document_source(kb_dir, child.source_id)["content"].strip() == (
        "Login requires user and pwd."
    )
    assert (
        next(job for job in result["jobs"] if job.get("filename") == "api.htm")["status"]
        == "completed"
    ), result


def test_runtime_reports_not_imported_as_skipped(
    kb_dir, writer_document, model_context, monkeypatch
):
    from native_catalog_fixtures import list_sources

    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.pending import claim_pending_job, process_pending, run_pending_job
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import RunPendingJob
    from ultrakb.runtime.worker import _execute

    with ZipFile(writer_document, "a") as package:
        package.writestr("word/embeddings/empty.htm", "<html><body></body></html>")
    parent = import_document(kb_dir, writer_document, context=model_context)
    process_pending(kb_dir, max_jobs=1, context=model_context)
    job = claim_pending_job(kb_dir)
    outcome = run_pending_job(kb_dir, job["id"], job["dispatch_id"], context=model_context)
    assert outcome["status"] == "not_imported", json.dumps(outcome, indent=2)
    # Replay the real storage-boundary result through the worker's status projection.
    monkeypatch.setattr("ultrakb.application.pending.run_pending_job", lambda *a, **k: outcome)
    result = _execute(
        RunPendingJob(job["id"], job["dispatch_id"]),
        UnitIdentity("a" * 32, "1", str(kb_dir), "b" * 64),
        ExecutionContext(),
    )
    assert result.status == "skipped" and "no readable body" in result.error, result
    assert result.model_usage["current"]["requests"] == 0
    assert [source.source_id for source in list_sources(kb_dir)] == [parent.source_id]


@pytest.fixture(autouse=True)
def html_model_transport(kb_dir):
    import json

    from stream_transport import StreamTransport

    from ultrakb.config import save_config

    save_config(
        kb_dir / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "pdf_short_max_pages": 20,
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            "model_policy": {"max_calls": 12, "retries": 0},
        },
    )

    def respond(request, index):
        return {
            "content": json.dumps(
                {"concepts": [], "entities": []}
                if index % 2
                else {"description": "Document", "content": "Login requires user and pwd."}
            )
        }

    with StreamTransport(respond) as service:
        (kb_dir / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        yield service


@pytest.fixture
def model_context(html_model_transport):
    from ultrakb.application.execution import ExecutionContext

    return ExecutionContext()
