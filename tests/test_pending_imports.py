"""Recovered complete files enter the ordinary Source pipeline through durable jobs."""

import json
from collections import Counter

import pytest

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


@pytest.mark.parametrize("mode", ["valid", "normalized", "repaired", "malformed"])
def test_plan_contract_controls(
    kb_dir, embedded_docx, office_runtime, pdf_model, monkeypatch, mode
):
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending

    calls = Counter()

    def response(model, messages, step, **kwargs):
        calls[step] += 1
        if step == "concepts-plan":
            valid = {"concepts": {}, "entities": {}}
            if mode == "malformed" or mode == "repaired" and calls[step] % 2:
                plan = {"concepts": "malformed"}
            elif mode == "normalized":
                plan = {"concepts": [], "entities": []}
            else:
                plan = valid
            return json.dumps(plan)
        return json.dumps({"description": "Fixture", "content": "# Fixture\n\nSource facts."})

    async def page(*args, **kwargs):
        return response(*args, **kwargs)

    monkeypatch.setattr("openkb.agent.compiler._llm_call", response)
    monkeypatch.setattr("openkb.agent.compiler._llm_call_page_async", page)
    parent = import_document(kb_dir, embedded_docx)
    drained = process_pending(kb_dir)
    child = next(j for j in pending_status(kb_dir)["jobs"] if j["kind"] == "import")
    receipt = child["result"]
    after = process_pending(kb_dir)
    reread = next(j for j in pending_status(kb_dir)["jobs"] if j["kind"] == "import")
    record = {
        "mode": mode,
        "parent_status": parent.status,
        "parent_quality": list(parent.quality),
        "parent_unfinished": list(parent.unfinished),
        "pending_status": child["status"],
        "pending_quality_known": receipt["quality_known"],
        "pending_quality": receipt["quality"],
        "pending_unfinished": receipt["unfinished"],
        "plan_calls": calls["concepts-plan"],
        "receipt_unchanged_after_readback": reread["result"] == receipt,
        "redrain_processed": after["processed"],
        "initial_processed": drained["processed"],
        "unit_statuses": [u["status"] for u in receipt["units"]],
        "published_unit_count": sum(bool(u.get("knowledge_revision_id")) for u in receipt["units"]),
        "model_boundary": "pdf_model and compiler stubs; no online generation",
    }
    expected_status = "failed" if mode == "malformed" else "completed"
    expected_quality = {
        "valid": [],
        "normalized": ["compile_plan_normalized"],
        "repaired": ["compile_plan_repaired"],
        "malformed": ["malformed_plan_items"],
    }[mode]
    assert child["status"] == expected_status, record
    assert parent.status == ("failed" if mode == "malformed" else "added"), record
    assert receipt["quality"] == list(parent.quality) == expected_quality, record
    assert (
        receipt["unfinished"]
        == list(parent.unfinished)
        == (["concepts", "entities"] if mode == "malformed" else [])
    ), record
    assert receipt["quality_known"] is True, record
    assert record["receipt_unchanged_after_readback"] and after["processed"] == 0, record
    assert calls["concepts-plan"] == (4 if mode in {"repaired", "malformed"} else 2), record
    assert record["published_unit_count"] == (0 if mode == "malformed" else 1), record


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
