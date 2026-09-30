"""Bounded dispatcher shared by explicit CLI drains and the resident desktop runtime."""

from pathlib import Path

from openkb.application.execution import ExecutionContext
from openkb.lifecycle import read_lifecycle
from openkb.locks import LockCancelled, kb_ingest_lock, kb_read_lock
from openkb.mutation import RecoveryRequired, mutation_scope
from openkb.pending.records import ExecutionBudget, ExecutionGroup
from openkb.pending.store import claim, group_for, jobs, read_job, reconcile_job, save_job, start
from openkb.source_catalog import read_record, record_path, write_record


def pending_status(kb_dir: Path) -> dict:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        entries = list(jobs(root))
        groups = {job.root_import_id: group_for(root, job) for _, job in entries}
        rows = [
            {"id": job.intent_id, "kind": kind, **job.model_dump(mode="json")}
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
            "groups": [group.model_dump(mode="json") for group in groups.values()],
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


def claim_pending_job(kb_dir: Path):
    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
        selected = claim(root)
        if selected:
            kind, job = selected
            return {"id": job.intent_id, "kind": kind, "dispatch_id": job.dispatch_id}
    return None


def run_pending_job(kb_dir: Path, identity: str, dispatch_id: str, *, context=None):
    root = kb_dir.resolve()
    context = context or ExecutionContext()
    with (
        read_lifecycle(root, cancelled=context.cancelled, on_wait=context.waiting),
        kb_ingest_lock(root / ".openkb", cancelled=context.cancelled, on_wait=context.waiting),
    ):
        from openkb.pending.control import group_cancelled, job_stopped, request_stop

        kind, before = read_job(root, identity)
        if context.cancelled() and before.dispatch_id == dispatch_id:
            request_stop(root, identity, dispatch_id, before.kb_generation)
        before = reconcile_job(root, kind, before)
        if before.status in {"cancelled", "stopped", "stale"}:
            return {"id": identity, "status": before.status}
        selected = start(root, identity, dispatch_id)
        if selected is None:
            return {"id": identity, "status": "skipped"}
        kind, job = selected
        original_cancelled = context.cancelled
        context.cancelled = (
            lambda: original_cancelled() or group_cancelled(root, job) or job_stopped(root, job)
        )
        try:
            if kind == "discovery":
                from openkb.pending.discovery import discover

                job = discover(root, job, context.check_stop)
            else:
                from openkb.pending.imports import import_file

                job = import_file(root, job, context)
        except RecoveryRequired:
            raise
        except (Exception, LockCancelled) as exc:
            # Read the last committed cursor/source before recording the execution result.
            _, job = read_job(root, identity)
            job = reconcile_job(root, kind, job)
            if job.status not in {"completed", "cancelled"}:
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
        return {"id": job.intent_id, "kind": kind, "status": job.status, "message": job.message}


def process_pending(kb_dir: Path, *, max_jobs: int = 10000, context=None) -> dict:
    if type(max_jobs) is not int or not 1 <= max_jobs <= 10000:
        raise ValueError("Choose 1–10000 jobs per drain")
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


def retry_pending_job(kb_dir: Path, identity: str) -> None:
    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
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
                }
            ),
        )


def update_execution_budget(kb_dir: Path, identity: str, patch: dict) -> dict:
    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
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


def cancel_execution_group(kb_dir: Path, identity: str) -> None:
    root = kb_dir.resolve()
    from openkb.pending.control import request_group_cancel

    # Atomic records can be read without the business lease; this signal must
    # reach a running worker before waiting for its write lease to be released.
    with read_lifecycle(root):
        group = read_record(root, "execution-groups", identity, ExecutionGroup)
        request_group_cancel(root, group)
        with kb_ingest_lock(root / ".openkb"):
            group = read_record(root, "execution-groups", identity, ExecutionGroup)
            records = {
                record_path(root, "execution-groups", identity): group.model_copy(
                    update={"cancelled": True}
                )
            }
            for kind, job in jobs(root):
                job = reconcile_job(root, kind, job)
                if job.root_import_id == identity and job.status != "completed":
                    collection = "discovery-intents" if kind == "discovery" else "import-intents"
                    records[record_path(root, collection, job.intent_id)] = job.model_copy(
                        update={"status": "cancelled", "cancelled": True}
                    )
            with mutation_scope(root, list(records), operation="cancel-execution-group"):
                for path, record in records.items():
                    write_record(path, record)
