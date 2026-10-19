"""Recovered complete files enter the ordinary Source pipeline through durable jobs."""

import json

import pytest
from test_pending_empty_html import html_model_transport  # noqa: F401

pytest_plugins = ("pending_fixtures",)
pytestmark = pytest.mark.integration


def test_frozen_container_discovers_one_actual_object_and_imports_an_independent_source(
    kb_dir, embedded_docx, office_runtime
):
    from native_catalog_fixtures import list_sources

    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import pending_status, process_pending, read_pending_job
    from ultrakb.application.removal import remove_document
    from ultrakb.documents import read_document_source

    parent = import_document(
        kb_dir,
        embedded_docx,
    )
    assert parent.status == "added" and parent.discovery_pending == 1, parent.message
    remove_document(kb_dir, parent.source_id)
    embedded_docx.unlink()
    drained = process_pending(kb_dir)
    assert drained["processed"] == 3  # Outer discovery, ordinary import, nested discovery.
    assert pending_status(kb_dir)["runnable"] == 0
    recovered = next(
        source for source in list_sources(kb_dir) if source.source_id != parent.source_id
    )
    body = read_document_source(kb_dir, recovered.source_id)
    assert recovered.label == "Recovered.docx" and "First section hello" in body["content"]
    assert "version_metadata" not in body
    inventory = pending_status(kb_dir)
    receipt_job = next(job for job in inventory["jobs"] if job["filename"] == "Recovered.docx")
    receipt = read_pending_job(kb_dir, receipt_job["id"])["result"]
    assert receipt_job["quality_known"] is True and receipt["quality"] == ()
    assert receipt["model_usage"]["current"]["requests"] > 0
    assert inventory["groups"][0]["model_usage"]["requests"] == (
        parent.model_usage["current"]["requests"] + receipt["model_usage"]["current"]["requests"]
    )
    assert process_pending(kb_dir)["processed"] == 0


def test_budget_raise_resumes_checkpoint_and_group_cancel_is_durable(kb_dir, embedded_docx):
    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import (
        cancel_execution_group,
        pending_status,
        process_pending,
        update_execution_budget,
    )

    import_document(kb_dir, embedded_docx)
    group = pending_status(kb_dir)["groups"][0]
    update_execution_budget(kb_dir, group["root_import_id"], {"max_sources": 1})
    assert process_pending(kb_dir)["processed"] == 1
    blocked = pending_status(kb_dir)
    assert blocked["jobs"][0]["status"] == "budget_wait"
    assert process_pending(kb_dir)["processed"] == 0
    update_execution_budget(kb_dir, group["root_import_id"], {"max_sources": 2})
    assert process_pending(kb_dir, max_jobs=1)["imports_pending"] == 1
    cancel_execution_group(kb_dir, group["root_import_id"])
    assert process_pending(kb_dir)["processed"] == 0
    assert pending_status(kb_dir)["groups"][0]["cancelled"]


def test_failed_import_receipt_stays_with_its_attempt_when_retried(
    kb_dir, embedded_docx, office_runtime, monkeypatch
):
    from native_catalog_fixtures import catalog

    from ultrakb.agent import compiler
    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import (
        pending_status,
        process_pending,
        read_pending_job,
        retry_pending_job,
    )
    from ultrakb.storage.pending_store import PendingStore

    import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    publish_summary = compiler._write_summary

    def unavailable(*args, **kwargs):
        raise OSError("Summary publication is unavailable")

    monkeypatch.setattr(compiler, "_write_summary", unavailable)
    process_pending(kb_dir, max_jobs=1)
    job = next(job for job in pending_status(kb_dir)["jobs"] if job["filename"] == "Recovered.docx")
    job = read_pending_job(kb_dir, job["id"])
    assert job["status"] in {"failed", "partial", "interrupted"}
    assert job["result"] is not None
    attempts = PendingStore(kb_dir, catalog(kb_dir).identity)
    previous = next(
        attempt
        for attempt in attempts.attempts(job["id"])
        if attempt.attempt_id == job["attempt_id"]
    )
    monkeypatch.setattr(compiler, "_write_summary", publish_summary)
    retry_pending_job(kb_dir, job["id"])
    queued = next(row for row in pending_status(kb_dir)["jobs"] if row["id"] == job["id"])
    assert queued["result"] is None and queued["quality_known"] is False
    process_pending(kb_dir)
    completed = read_pending_job(kb_dir, job["id"])
    assert completed["status"] == "completed"
    assert completed["attempt_id"] != previous.attempt_id
    assert (
        next(
            attempt
            for attempt in attempts.attempts(job["id"])
            if attempt.attempt_id == previous.attempt_id
        )
        == previous
    )


def test_pending_receipt_validates_units_and_their_model_usage():
    from ultrakb.pending.records import PendingImportResult

    receipt = {
        "attempt_id": "a" * 32,
        "source_id": "b" * 32,
        "source_revision_id": "c" * 32,
        "quality_known": True,
        "units": [],
    }
    valid = {"unit_id": "d" * 32, "status": "completed", "target_revision_id": "e" * 32}
    for invalid in (
        {},
        valid | {"model_usage": {}},
        valid | {"unit_id": 13},
        valid | {"pages": True},
    ):
        with pytest.raises(ValueError):
            PendingImportResult.model_validate_json(json.dumps(receipt | {"units": [invalid]}))
    parsed = PendingImportResult.model_validate_json(json.dumps(receipt | {"units": [valid]}))
    assert parsed.model_dump(mode="json")["units"][0]["unit_id"] == valid["unit_id"]
