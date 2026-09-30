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
    assert parent.status == "added" and parent.discovery_pending == 1
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
