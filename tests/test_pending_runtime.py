"""A real spawn worker runs a claimed job; task-history cleanup leaves durable work."""

pytest_plugins = ("pending_fixtures",)


def test_spawn_discovery_and_history_cleanup_keep_committed_import_intents(
    kb_dir, embedded_docx, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.application.pending import claim_pending_job, pending_status
    from openkb.runtime.requests import RunPendingJob
    from openkb.runtime.tasks import TaskManager

    import_document(kb_dir, embedded_docx)
    job = claim_pending_job(kb_dir)
    manager = TaskManager(history_dir=kb_dir / "history", max_workers=1)
    try:
        task = manager.submit(kb_dir, [RunPendingJob(job["id"], job["dispatch_id"])])
        result = manager.wait(task, timeout=30)
        assert result.state == "completed", result
        manager.clear_history([task])
        assert pending_status(kb_dir)["imports_pending"] == 1
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_admission_freezes_effective_budget_and_reports_its_origin(
    kb_dir, embedded_docx, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"extraction_budget": {"max_sources": 1}}),
    )
    import_document(kb_dir, embedded_docx)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"extraction_budget": {"max_sources": 2}}),
    )
    assert read_kb_config(kb_dir).extraction_budget.max_sources == 2
    process_pending(kb_dir)
    group = pending_status(kb_dir)["groups"][0]
    assert group["budget"]["max_sources"] == 1 and group["budget_origin"] == "kb"


def test_stopping_not_started_job_is_not_redispatched(kb_dir, embedded_docx, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.pending import claim_pending_job, pending_status
    from openkb.locks import kb_ingest_lock
    from openkb.runtime.requests import RunPendingJob
    from openkb.runtime.tasks import TaskManager

    import_document(kb_dir, embedded_docx)
    job = claim_pending_job(kb_dir)
    manager = TaskManager(history_dir=kb_dir / "history", max_workers=1)
    try:
        with kb_ingest_lock(kb_dir / ".openkb"):
            task = manager.submit(kb_dir, [RunPendingJob(job["id"], job["dispatch_id"])])
            manager.stop(task)
        result = manager.wait(task, timeout=30)
        assert result.state == "stopped", result
        assert claim_pending_job(kb_dir) is None, pending_status(kb_dir)
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_stopping_stale_pending_task_still_stops_the_runtime(kb_dir, embedded_docx, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.application.pending import claim_pending_job
    from openkb.kb_admin import delete_kb
    from openkb.lifecycle import exclusive_lifecycle
    from openkb.runtime.requests import RunPendingJob
    from openkb.runtime.tasks import TaskManager

    import_document(kb_dir, embedded_docx)
    job = claim_pending_job(kb_dir)
    manager = TaskManager(history_dir=kb_dir.with_name(kb_dir.name + "-history"))
    try:
        with exclusive_lifecycle(kb_dir):
            task = manager.submit(kb_dir, [RunPendingJob(job["id"], job["dispatch_id"])])
            delete_kb(kb_dir)
            initialize_kb(kb_dir, seed_environment=False)
            manager.stop(task)
        assert manager.wait(task, timeout=20).state in {"stopped", "blocked"}
        assert not (kb_dir / ".openkb/pending-control").exists()
    finally:
        if "task" in locals():
            manager.wait(task, timeout=20)
        manager.shutdown(stop=True)
        assert manager.join(20)
