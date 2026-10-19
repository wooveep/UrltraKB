"""Bounded dispatcher shared by explicit CLI drains and the resident desktop runtime."""

from pathlib import Path

from ultrakb.application.execution import ExecutionContext
from ultrakb.lifecycle import read_lifecycle
from ultrakb.llm_usage_execution import track_import_usage
from ultrakb.locks import LockCancelled, kb_ingest_lock, kb_read_lock
from ultrakb.mutation import RecoveryRequired, mutation_scope
from ultrakb.pending.records import DiscoveryCheckpoint, ExecutionBudget, ExecutionGroup
from ultrakb.pending.store import claim, group_for, jobs, read_job, reconcile_job, save_job, start
from ultrakb.source_catalog import read_record, record_path, write_record


def pending_result(kind, job, kb_dir):
    snapshot = job.result if kind == "import" else None
    from ultrakb.llm_usage_execution import pending_usage_receipt

    return {
        "id": job.intent_id,
        "kind": kind,
        "status": job.status,
        "message": job.message,
        "source_id": getattr(job, "source_id", None),
        "source_revision_id": getattr(job, "source_revision_id", None),
        "result": snapshot.model_dump(mode="json") if snapshot else None,
        "quality_known": snapshot.quality_known if snapshot else False,
        "quality": list(snapshot.quality) if snapshot else [],
        "unfinished": list(snapshot.unfinished) if snapshot else [],
        "model_usage": snapshot.model_usage
        if snapshot
        else pending_usage_receipt(
            kb_dir,
            job.intent_id,
            job.attempt_id,
        ),
    }


def _main_scope(kb_dir, scope):
    from ultrakb.knowledge_scope import resolve_scope

    selected = resolve_scope(kb_dir, scope) if scope is not None else None
    return selected if selected is not None and selected.knowledge is not None else None


def pending_status(kb_dir: Path, *, scope=None, after=0, limit=63) -> dict:
    root = kb_dir.resolve()
    if selected := _main_scope(root, scope):
        from .storage_pending_queries import stored_pending_status

        return stored_pending_status(selected, after=after, limit=limit)
    with kb_read_lock(root / ".ultrakb"):
        entries = list(jobs(root))
        groups = {job.root_import_id: group_for(root, job) for _, job in entries}
        rows = [
            {**job.model_dump(mode="json"), **pending_result(kind, job, root)}
            for kind, job in entries
        ]
        active = {
            "pending",
            "dispatched",
            "started",
            "budget_wait",
            "blocked",
            "interrupted",
            "partial",
            "failed",
            "stopped",
        }
        return {
            "jobs": rows,
            "checkpoints": [
                read_record(
                    root, "discovery-checkpoints", path.stem, DiscoveryCheckpoint
                ).model_dump(mode="json")
                for path in sorted((root / ".ultrakb/catalog/discovery-checkpoints").glob("*.json"))
            ],
            "groups": [_group_result(root, group) for group in groups.values()],
            "runnable": sum(
                job.status in {"pending", "dispatched"}
                and not job.cancelled
                and not groups[job.root_import_id].cancelled
                for _, job in entries
            ),
            "discovery_pending": sum(
                kind == "discovery" and job.status in active for kind, job in entries
            ),
            "imports_pending": sum(
                kind == "import" and job.status in active for kind, job in entries
            ),
        }


def _group_result(root, group):
    from ultrakb.llm_usage import aggregate_usage

    return {
        **group.model_dump(mode="json"),
        "model_usage": aggregate_usage(root, root_import_id=group.root_import_id),
        "usage_ledger": str(root / ".ultrakb/usage"),
    }


def read_pending_job(kb_dir: Path, identity: str, *, scope=None, checkpoint_after=0, limit=63):
    root = kb_dir.resolve()
    if selected := _main_scope(root, scope):
        from .storage_pending_queries import stored_job

        return stored_job(
            selected, identity, details=True, checkpoint_after=checkpoint_after, limit=limit
        )
    with kb_read_lock(root / ".ultrakb"):
        kind, job = read_job(root, identity)
        return {**job.model_dump(mode="json"), **pending_result(kind, job, root)}


def claim_pending_job(kb_dir: Path, *, scope=None, cancelled=None):
    root = kb_dir.resolve()
    if selected := _main_scope(root, scope):
        from .storage_pending import claim_stored_job

        return claim_stored_job(selected, cancelled=cancelled)
    with kb_ingest_lock(root / ".ultrakb"):
        selected = claim(root)
        if selected:
            kind, job = selected
            return {"id": job.intent_id, "kind": kind, "dispatch_id": job.dispatch_id}
    return None


def stop_pending_job(kb_dir: Path, identity: str, dispatch_id: str, generation: str, *, scope=None):
    from ultrakb.lifecycle import expected_generation

    root = kb_dir.resolve()
    with expected_generation(root, generation):
        if selected := _main_scope(root, scope):
            from ultrakb.storage.pending_store import PendingStore

            return PendingStore(root, selected.knowledge.identity).stop(identity, dispatch_id)
        from ultrakb.pending.control import request_stop

        return request_stop(root, identity, dispatch_id, generation)


@track_import_usage
def run_pending_job(kb_dir: Path, identity: str, dispatch_id: str, *, context=None, scope=None):
    root = kb_dir.resolve()
    if selected := _main_scope(root, scope):
        from .storage_pending import run_stored_job

        return run_stored_job(selected, identity, dispatch_id, context)
    context = context or ExecutionContext()
    with (
        read_lifecycle(root, cancelled=context.cancelled, on_wait=context.waiting),
        kb_ingest_lock(root / ".ultrakb", cancelled=context.cancelled, on_wait=context.waiting),
    ):
        from ultrakb.pending.control import group_cancelled, job_stopped, request_stop

        kind, before = read_job(root, identity)
        if context.cancelled() and before.dispatch_id == dispatch_id:
            request_stop(root, identity, dispatch_id, before.kb_generation)
        before = reconcile_job(root, kind, before)
        if before.status in {"cancelled", "stopped", "stale"}:
            return {"id": identity, "status": before.status}
        selected = start(root, identity, dispatch_id)
        if selected is None:
            return {**pending_result(kind, before, root), "status": "skipped"}
        kind, job = selected
        from ultrakb.llm_usage_execution import bind_pending_execution

        bind_pending_execution(job.intent_id, job.attempt_id)
        original_cancelled = context.cancelled
        context.cancelled = (
            lambda: original_cancelled() or group_cancelled(root, job) or job_stopped(root, job)
        )
        try:
            if kind == "discovery":
                from ultrakb.pending.discovery import discover

                job = discover(root, job, context.check_stop)
            else:
                from ultrakb.pending.imports import import_file

                job = import_file(root, job, context)
        except RecoveryRequired:
            raise
        except (Exception, LockCancelled) as exc:
            # Read the last committed cursor/source before recording the execution result.
            _, job = read_job(root, identity)
            job = reconcile_job(root, kind, job)
            if job.status not in {"completed", "not_imported", "cancelled"}:
                job = save_job(
                    root,
                    kind,
                    job.model_copy(
                        update={
                            "status": "stopped" if isinstance(exc, LockCancelled) else "failed",
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    ),
                )
            if isinstance(exc, LockCancelled) and original_cancelled():
                raise
        finally:
            context.cancelled = original_cancelled
        return pending_result(kind, job, root)


def process_pending(kb_dir: Path, *, max_jobs: int = 10000, context=None, scope=None) -> dict:
    if type(max_jobs) is not int or not 1 <= max_jobs <= 10000:
        raise ValueError("Choose 1–10000 jobs per drain")
    if selected := _main_scope(kb_dir.resolve(), scope):
        from .storage_pending import process_stored_pending

        return process_stored_pending(selected, max_jobs=max_jobs, context=context)
    context = context or ExecutionContext()
    processed = 0
    outcomes = []
    while processed < max_jobs:
        context.check_stop()
        selected = claim_pending_job(kb_dir)
        if selected is None:
            break
        outcomes.append(
            run_pending_job(kb_dir, selected["id"], selected["dispatch_id"], context=context)
        )
        processed += 1
    return {"processed": processed, "outcomes": outcomes, **pending_status(kb_dir)}


def retry_pending_job(kb_dir: Path, identity: str, *, scope=None) -> None:
    root = kb_dir.resolve()
    if selected := _main_scope(root, scope):
        from ultrakb.storage.pending_store import PendingStore

        PendingStore(root, selected.knowledge.identity).retry(identity)
        return
    with kb_ingest_lock(root / ".ultrakb"):
        kind, job = read_job(root, identity)
        job = reconcile_job(root, kind, job)
        if job.status not in {"failed", "partial", "interrupted", "stopped"}:
            raise ValueError(
                "Only failed, partial, interrupted or stopped jobs can be explicitly retried"
            )
        save_job(
            root,
            kind,
            job.model_copy(
                update={
                    "status": "pending",
                    "dispatch_id": None,
                    "attempt_id": None,
                    "message": None,
                    **({"result": None} if kind == "import" else {}),
                }
            ),
        )


def update_execution_budget(kb_dir: Path, identity: str, patch: dict, *, scope=None) -> dict:
    root = kb_dir.resolve()
    if selected := _main_scope(root, scope):
        from ultrakb.storage.discovery_store import DiscoveryStore

        return DiscoveryStore(root, selected.knowledge.identity).update_budget(identity, patch)
    with kb_ingest_lock(root / ".ultrakb"):
        group = read_record(root, "execution-groups", identity, ExecutionGroup)
        budget = ExecutionBudget.model_validate({**group.budget.model_dump(), **patch})
        if group.cancelled:
            raise ValueError("A cancelled execution group cannot resume")
        changed = group.model_copy(update={"budget": budget, "budget_origin": "execution_group"})
        records = {record_path(root, "execution-groups", identity): changed}
        raised = any(
            getattr(budget, field) > getattr(group.budget, field)
            for field in type(budget).model_fields
        )
        if raised:
            for kind, job in jobs(root):
                if job.root_import_id == identity and job.status == "budget_wait":
                    collection = "discovery-intents" if kind == "discovery" else "import-intents"
                    records[record_path(root, collection, job.intent_id)] = job.model_copy(
                        update={
                            "status": "pending",
                            "dispatch_id": None,
                            "attempt_id": None,
                            "message": None,
                        }
                    )
        with mutation_scope(root, list(records), operation="update-execution-budget"):
            for path, record in records.items():
                write_record(path, record)
        return changed.model_dump(mode="json")


def cancel_execution_group(kb_dir: Path, identity: str, *, scope=None) -> None:
    root = kb_dir.resolve()
    if selected := _main_scope(root, scope):
        from ultrakb.storage.pending_store import PendingStore

        PendingStore(root, selected.knowledge.identity).cancel(identity)
        return
    from ultrakb.pending.control import request_group_cancel

    # Atomic records can be read without the business lease; this signal must
    # reach a running worker before waiting for its write lease to be released.
    with read_lifecycle(root):
        group = read_record(root, "execution-groups", identity, ExecutionGroup)
        request_group_cancel(root, group)
        with kb_ingest_lock(root / ".ultrakb"):
            group = read_record(root, "execution-groups", identity, ExecutionGroup)
            records = {
                record_path(root, "execution-groups", identity): group.model_copy(
                    update={"cancelled": True}
                )
            }
            for kind, job in jobs(root):
                job = reconcile_job(root, kind, job)
                if job.root_import_id == identity and job.status not in {
                    "completed",
                    "not_imported",
                }:
                    collection = "discovery-intents" if kind == "discovery" else "import-intents"
                    records[record_path(root, collection, job.intent_id)] = job.model_copy(
                        update={"status": "cancelled", "cancelled": True}
                    )
            with mutation_scope(root, list(records), operation="cancel-execution-group"):
                for path, record in records.items():
                    write_record(path, record)
