"""Recovered complete files enter the ordinary Source pipeline through durable jobs."""

import json

import pytest

pytest_plugins = ("pending_fixtures",)


def test_frozen_container_discovers_one_actual_object_and_imports_an_independent_source(
    kb_dir, embedded_docx, office_runtime, pdf_model
):
    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import pending_status, process_pending
    from ultrakb.application.removal import remove_document
    from ultrakb.documents import read_document_source
    from ultrakb.source_catalog import list_sources

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
    assert recovered.name == "Recovered.docx" and "First section hello" in body["content"]
    assert "version_metadata" not in body
    inventory = pending_status(kb_dir)
    receipt = next(job for job in inventory["jobs"] if job["kind"] == "import")["result"]
    assert receipt["quality_known"] is True and receipt["quality"] == []
    assert receipt["model_usage"]["current"]["requests"] > 0
    assert inventory["groups"][0]["model_usage"]["requests"] == (
        parent.model_usage["current"]["requests"] + receipt["model_usage"]["current"]["requests"]
    )
    assert process_pending(kb_dir)["processed"] == 0


def test_checkpoint_failure_commits_neither_cursor_budget_nor_import_intent(
    kb_dir, embedded_docx, pdf_model, monkeypatch
):
    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import pending_status, process_pending, retry_pending_job
    from ultrakb.mutation import MutationSnapshot

    import_document(kb_dir, embedded_docx)
    commit = MutationSnapshot.mark_committed

    def unavailable(snapshot):
        if snapshot.operation == "discovery-checkpoint":
            raise OSError("Fixture unavailable commit marker")
        commit(snapshot)

    monkeypatch.setattr(MutationSnapshot, "mark_committed", unavailable)
    process_pending(kb_dir)
    inventory = pending_status(kb_dir)
    assert inventory["imports_pending"] == 0
    discovery = next(job for job in inventory["jobs"] if job["kind"] == "discovery")
    assert discovery["cursor"] == 0 and discovery["status"] == "failed"
    assert inventory["groups"][0]["sources"] == 1
    monkeypatch.setattr(MutationSnapshot, "mark_committed", commit)
    retry_pending_job(kb_dir, discovery["id"])
    assert process_pending(kb_dir, max_jobs=1)["imports_pending"] == 1


def test_budget_raise_resumes_checkpoint_and_group_cancel_is_durable(
    kb_dir, embedded_docx, pdf_model
):
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


@pytest.mark.parametrize("mode", ["valid", "normalized", "repaired", "malformed"])
def test_plan_contract_controls(kb_dir, mode):
    from ultrakb.agent.compile_plan import normalize_plan
    from ultrakb.agent.compiler import _sanitize_concept_name
    from ultrakb.application.pending import pending_result
    from ultrakb.compilation_report import CompileReport
    from ultrakb.ingest_result import ImportUnitOutcome, IngestResult
    from ultrakb.pending.imports import build_pending_import_result
    from ultrakb.pending.records import JobAttempt
    from ultrakb.source_catalog import read_record, record_path, write_record

    options = dict(
        existing={"concepts": set(), "entities": set()},
        entity_types=frozenset({"other"}),
        sanitize=_sanitize_concept_name,
    )
    raw = (
        {"concepts": [], "entities": []}
        if mode == "normalized"
        else {"concepts": {}, "entities": {}}
    )
    if mode in {"malformed", "repaired"}:
        with pytest.raises(ValueError):
            normalize_plan({"concepts": "malformed"}, **options)
    if mode != "malformed":
        plan = normalize_plan(raw, **options)
        assert plan.normalized == (mode == "normalized")
    expected_quality = {
        "valid": [],
        "normalized": ["compile_plan_normalized"],
        "repaired": ["compile_plan_repaired"],
        "malformed": ["malformed_plan_items"],
    }[mode]
    unfinished = ["concepts", "entities"] if mode == "malformed" else []
    successful = mode != "malformed"
    delivered = IngestResult(
        "recovered:source",
        "added" if successful else "failed",
        (),
        quality=tuple(expected_quality),
        unfinished=tuple(unfinished),
        units=(
            ImportUnitOutcome(
                unit_id="a" * 32,
                target_revision_id="b" * 32,
                status="completed" if successful else "failed",
                knowledge_revision_id="c" * 32 if successful else None,
            ),
        ),
    )
    receipt = build_pending_import_result(
        delivered,
        CompileReport(quality=list(expected_quality), unfinished=unfinished),
        attempt_id="d" * 32,
        source_id="e" * 32,
        source_revision_id="f" * 32,
    )
    job = JobAttempt(
        attempt_id="d" * 32,
        intent_id="1" * 32,
        kind="import",
        status="completed" if successful else "failed",
        result=receipt,
    )
    write_record(record_path(kb_dir, "pending-attempts", job.attempt_id), job)
    reread = read_record(kb_dir, "pending-attempts", job.attempt_id, JobAttempt)
    public = pending_result("import", reread, kb_dir)
    assert reread == job and reread.result == receipt
    assert public["quality_known"] is True
    assert public["quality"] == expected_quality
    assert public["unfinished"] == unfinished
    assert public["status"] == ("completed" if successful else "failed")
    assert sum(bool(unit.knowledge_revision_id) for unit in receipt.units) == int(successful)
    assert pending_result("import", reread, kb_dir) == public


def test_failed_import_receipt_stays_with_its_attempt_when_retried(
    kb_dir, embedded_docx, office_runtime, pdf_model, monkeypatch
):
    from ultrakb.agent import compiler
    from ultrakb.application.documents import import_document
    from ultrakb.application.pending import pending_status, process_pending, retry_pending_job
    from ultrakb.pending.records import JobAttempt
    from ultrakb.source_catalog import read_record

    import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    publish_summary = compiler._write_summary

    def unavailable(*args, **kwargs):
        raise OSError("Summary publication is unavailable")

    monkeypatch.setattr(compiler, "_write_summary", unavailable)
    process_pending(kb_dir, max_jobs=1)
    job = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert job["status"] in {"failed", "partial", "interrupted"}
    assert job["result"] is not None
    previous = read_record(kb_dir, "pending-attempts", job["attempt_id"], JobAttempt)
    monkeypatch.setattr(compiler, "_write_summary", publish_summary)
    retry_pending_job(kb_dir, job["id"])
    queued = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert queued["result"] is None and queued["quality_known"] is False
    process_pending(kb_dir)
    completed = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert completed["status"] == "completed"
    assert completed["attempt_id"] != previous.attempt_id
    assert read_record(kb_dir, "pending-attempts", previous.attempt_id, JobAttempt) == previous


def test_pending_receipt_validates_units_and_their_model_usage():
    import pytest

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
