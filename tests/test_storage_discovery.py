"""Real Office containers recover frozen objects through the main-store pending flow."""

import json
from zipfile import ZipFile

import pytest
from openpyxl import Workbook
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.pending import (
    cancel_execution_group,
    pending_status,
    process_pending,
    read_pending_job,
    retry_pending_job,
    update_execution_budget,
)
from ultrakb.application.removal import remove_document
from ultrakb.application.views import storage_scope
from ultrakb.storage.catalog_store import CatalogStore

pytestmark = pytest.mark.integration


def embedded_book(path, *, objects=1):
    book = Workbook()
    book.active["A1"] = "PARENT_BODY"
    book.save(path)
    with ZipFile(path, "a") as archive:
        for index in range(objects):
            archive.writestr(f"xl/embeddings/Recovered{index}.txt", "CHILD_BODY")
        if objects:
            archive.writestr(
                "xl/_rels/embedded.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="one" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/package" Target="embeddings/Recovered0.txt"/>'
                '<Relationship Id="two" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/package" Target="embeddings/Recovered0.txt"/>'
                "</Relationships>",
            )


def responses(*values):
    return [
        item
        for value in values
        for item in (
            {"content": json.dumps({"description": value, "content": value})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        )
    ]


def test_frozen_discovery_survives_parent_withdrawal_and_imports_an_independent_source(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    with StreamTransport(responses("PARENT_BODY", "CHILD_BODY")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        parent = import_document(root, source, scope=storage_scope(root, identity))
        assert parent.status == "added", parent.message
        assert parent.discovery_pending == 1
        removed = remove_document(root, parent.source_id, scope=storage_scope(root, identity))
        assert removed.status == "removed"
        source.unlink()
        scanned = process_pending(root, max_jobs=1, scope=storage_scope(root, identity))
        assert scanned["processed"] == 1 and scanned["imports_pending"] == 1
        inventory = pending_status(root, scope=storage_scope(root, identity))
        assert len(inventory["checkpoints"]) == 1
        assert inventory["checkpoints"][0]["outcome"] == "recovered"
        drained = process_pending(root, scope=storage_scope(root, identity))
        assert drained["processed"] == 1 and drained["runnable"] == 0
        catalog = CatalogStore(root, identity)
        [child] = [item for item in catalog.source_page() if item.source_id != parent.source_id]
        assert catalog.read_original(child.revision_id) == b"CHILD_BODY"
        current = storage_scope(root, identity)
        assert any(
            b"CHILD_BODY" in current.knowledge.read(path)
            for path in current.knowledge.file_page(prefix="summaries/")
        )
        inventory = pending_status(root, scope=current)
        result = read_pending_job(
            root, next(job["id"] for job in inventory["jobs"] if job["has_result"]), scope=current
        )
        assert result["result"]["source_id"] == child.source_id
        assert result["result"]["model_usage"]["current"]["actual_sends"] == 2
        assert inventory["groups"][0]["model_usage"]["actual_sends"] == 4
        assert len(service.requests) == 4
        assert process_pending(root, scope=current)["processed"] == 0
    assert not (root / ".ultrakb/catalog").exists()
    assert not (root / ".ultrakb/usage").exists()


@pytest.mark.parametrize("boundary", ["transaction", "lost_ack"])
def test_discovery_acceptance_keeps_cursor_payload_queue_and_object_budget_together(
    tmp_path, monkeypatch, boundary
):
    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.discovery_store import DiscoveryStore

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    with StreamTransport(responses("PARENT_BODY", "CHILD_BODY")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        import_document(root, source, scope=storage_scope(root, identity))
        if boundary == "transaction":
            with storage_stage(root, identity) as db:
                db.connection.execute(
                    "CREATE TRIGGER fail_discovery BEFORE UPDATE OF cursor ON ukb_discovery_runs "
                    "BEGIN SELECT RAISE(ABORT, 'cursor commit failed'); END"
                )
        accept = DiscoveryStore.accept

        def lost_ack(*args, **options):
            accept(*args, **options)
            raise OSError("Object acknowledgement lost")

        with monkeypatch.context() as fault:
            if boundary == "lost_ack":
                fault.setattr(DiscoveryStore, "accept", lost_ack)
            process_pending(root, max_jobs=1, scope=storage_scope(root, identity))
        inventory = pending_status(root, scope=storage_scope(root, identity))
        [discovery] = [job for job in inventory["jobs"] if job["kind"] == "discovery"]
        assert discovery["status"] == "failed"
        assert discovery["cursor"] == int(boundary == "lost_ack")
        assert inventory["imports_pending"] == int(boundary == "lost_ack")
        assert inventory["groups"][0]["sources"] == (2 if boundary == "lost_ack" else 1)
        assert inventory["groups"][0]["decompressed_bytes"] > 0
        if boundary == "transaction":
            with storage_stage(root, identity) as db:
                db.connection.execute("DROP TRIGGER fail_discovery")
        retry_pending_job(root, discovery["id"], scope=storage_scope(root, identity))
        finished = process_pending(root, scope=storage_scope(root, identity))
        assert finished["runnable"] == 0 and finished["imports_pending"] == 0
        assert len(finished["checkpoints"]) == 1
        assert finished["groups"][0]["sources"] == 2
        assert len(service.requests) == 4


def test_budget_resume_and_cancel_keep_delivered_objects_and_same_bytes_are_distinct_sources(
    tmp_path,
):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source, objects=2)
    with StreamTransport(responses("PARENT_BODY", "CHILD_BODY", "CHILD_BODY")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        parent = import_document(root, source, scope=storage_scope(root, identity))
        scope = storage_scope(root, identity)
        [group] = pending_status(root, scope=scope)["groups"]
        update_execution_budget(root, group["root_import_id"], {"max_sources": 2}, scope=scope)
        first = process_pending(root, max_jobs=1, scope=scope)
        assert first["jobs"][0]["status"] == "budget_wait"
        assert first["jobs"][0]["cursor"] == 1 and len(first["checkpoints"]) == 1
        assert first["groups"][0]["sources"] == 2
        assert process_pending(root, scope=scope)["processed"] == 1  # First child is independent.
        assert process_pending(root, scope=scope)["processed"] == 0
        update_execution_budget(
            root, group["root_import_id"], {"max_sources": 3}, scope=storage_scope(root, identity)
        )
        done = process_pending(root, scope=storage_scope(root, identity))
        assert done["runnable"] == 0 and len(done["checkpoints"]) == 2
        children = [
            item
            for item in CatalogStore(root, identity).source_page()
            if item.source_id != parent.source_id
        ]
        assert len(children) == 2 and children[0].source_id != children[1].source_id
        assert (
            len(
                {
                    CatalogStore(root, identity).read_original(child.revision_id)
                    for child in children
                }
            )
            == 1
        )
        cancel_execution_group(root, group["root_import_id"], scope=storage_scope(root, identity))
        assert process_pending(root, scope=storage_scope(root, identity))["processed"] == 0
        assert len(service.requests) == 6


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_child_import_result_receipt_failure_retains_publication_and_retry_does_not_send(
    tmp_path, monkeypatch, boundary
):
    from ultrakb.storage.pending_results import PendingResults

    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    with StreamTransport(responses("PARENT_BODY", "CHILD_BODY")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        import_document(root, source, scope=storage_scope(root, identity))
        process_pending(root, max_jobs=1, scope=storage_scope(root, identity))
        save = PendingResults.save

        def fail_receipt(*args, **options):
            if boundary == "after":
                save(*args, **options)
            raise OSError("Source import result receipt unavailable")

        with monkeypatch.context() as fault:
            fault.setattr(PendingResults, "save", fail_receipt)
            result = process_pending(root, scope=storage_scope(root, identity))
        [outcome] = result["outcomes"]
        assert outcome["result"]["status"] == "added"
        assert outcome["publication_id"] == storage_scope(root, identity).publication_id
        assert len(service.requests) == 4
        scope = storage_scope(root, identity)
        [job] = [
            job for job in pending_status(root, scope=scope)["jobs"] if job["kind"] == "import"
        ]
        if boundary == "before":
            assert job["status"] == "interrupted"
            retry_pending_job(root, job["id"], scope=scope)
            assert (
                process_pending(root, scope=scope)["outcomes"][0]["result"]["status"] == "skipped"
            )
        assert (
            read_pending_job(root, job["id"], scope=storage_scope(root, identity))["status"]
            == "completed"
        )
        assert len(service.requests) == 4
