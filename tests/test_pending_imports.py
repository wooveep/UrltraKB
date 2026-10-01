"""Recovered complete files enter the ordinary Source pipeline through durable jobs."""

pytest_plugins = ("pending_fixtures",)


def test_frozen_container_discovers_one_actual_object_and_imports_an_independent_source(
    kb_dir, embedded_docx, office_runtime, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending
    from openkb.application.removal import remove_document
    from openkb.documents import read_document_source
    from openkb.source_catalog import list_sources
    from openkb.view_records import SourceMetadata

    parent = import_document(
        kb_dir,
        embedded_docx,
        metadata=SourceMetadata(product="Container", family="Outer", applicable_versions=("9",)),
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
    assert body["version_metadata"]["product"] != "Container"
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
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending, retry_pending_job
    from openkb.mutation import MutationSnapshot

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
    from openkb.application.documents import import_document
    from openkb.application.pending import (
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


def test_pending_import_preserves_current_compile_warning_after_readback(
    kb_dir, embedded_docx, office_runtime, pdf_model, monkeypatch
):
    import json

    import litellm

    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending

    completion = litellm.completion

    def broken_plan(**kwargs):
        response = completion(**kwargs)
        if "concepts" in str(kwargs["messages"]).lower():
            response.choices[0].message.content = json.dumps({"concepts": "malformed"})
        return response

    async def abroken_plan(**kwargs):
        return broken_plan(**kwargs)

    monkeypatch.setattr(litellm, "completion", broken_plan)
    monkeypatch.setattr(litellm, "acompletion", abroken_plan)
    parent = import_document(kb_dir, embedded_docx)
    process_pending(kb_dir)
    imported = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert imported["status"] == "completed", imported["message"]
    assert imported["result"]["quality_known"] is True
    assert imported["result"]["quality"] == list(parent.quality)
    assert imported["result"]["quality"]
    assert imported["result"]["unfinished"]
    before = imported["result"]
    assert process_pending(kb_dir)["processed"] == 0
    assert (
        next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")["result"]
        == before
    )


def test_failed_import_receipt_stays_with_its_attempt_when_retried(
    kb_dir, embedded_docx, office_runtime, pdf_model, monkeypatch
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending, retry_pending_job
    from openkb.pending.records import JobAttempt
    from openkb.source_catalog import read_record

    import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    completion = litellm.completion

    def unavailable(**kwargs):
        raise ConnectionError("Fixture unavailable model")

    monkeypatch.setattr(litellm, "completion", unavailable)
    process_pending(kb_dir, max_jobs=1)
    job = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert job["status"] in {"failed", "partial", "interrupted"}
    assert job["result"] is not None
    previous = read_record(kb_dir, "pending-attempts", job["attempt_id"], JobAttempt)
    monkeypatch.setattr(litellm, "completion", completion)
    retry_pending_job(kb_dir, job["id"])
    queued = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert queued["result"] is None and queued["quality_known"] is False
    process_pending(kb_dir)
    completed = next(job for job in pending_status(kb_dir)["jobs"] if job["kind"] == "import")
    assert completed["status"] == "completed"
    assert completed["attempt_id"] != previous.attempt_id
    assert read_record(kb_dir, "pending-attempts", previous.attempt_id, JobAttempt) == previous


def test_pending_receipt_validates_units_and_their_model_usage():
    import json

    import pytest

    from openkb.pending.records import PendingImportResult

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
