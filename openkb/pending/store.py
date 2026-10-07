"""Authoritative intent state, claim tokens and immutable execution attempts."""

import uuid

from openkb.lifecycle import current_generation
from openkb.mutation import mutation_scope
from openkb.pending.control import group_cancelled, job_stopped
from openkb.pending.records import ExecutionGroup, ImportIntent, JobAttempt
from openkb.source_catalog import read_record, record_path, write_record
from openkb.source_records import DiscoveryIntent

MODELS = {
    "discovery": ("discovery-intents", DiscoveryIntent),
    "import": ("import-intents", ImportIntent),
}


def jobs(kb_dir):
    for kind, (collection, model) in MODELS.items():
        for path in sorted((kb_dir / ".openkb/catalog" / collection).glob("*.json")):
            yield kind, read_record(kb_dir, collection, path.stem, model)


def read_job(kb_dir, identity):
    for kind, (collection, model) in MODELS.items():
        if record_path(kb_dir, collection, identity).exists():
            return kind, read_record(kb_dir, collection, identity, model)
    raise ValueError("Pending job does not exist")


def group_for(kb_dir, job):
    path = record_path(kb_dir, "execution-groups", job.root_import_id)
    if path.exists():
        return read_record(kb_dir, "execution-groups", job.root_import_id, ExecutionGroup)
    # Older frozen admissions already own a root ID; materialize it only on a write.
    return ExecutionGroup(root_import_id=job.root_import_id, kb_generation=job.kb_generation)


def job_records(kb_dir, kind, job):
    records = {record_path(kb_dir, MODELS[kind][0], job.intent_id): job}
    if job.attempt_id:
        records[record_path(kb_dir, "pending-attempts", job.attempt_id)] = JobAttempt(
            attempt_id=job.attempt_id,
            intent_id=job.intent_id,
            kind=kind,
            status=job.status,
            message=job.message,
            result=job.result if kind == "import" else None,
        )
    return records


def save_job(kb_dir, kind, job, *, operation="update-pending-job"):
    records = job_records(kb_dir, kind, job)
    with mutation_scope(kb_dir, list(records), operation=operation):
        for path, record in records.items():
            write_record(path, record)
    return job


def import_source(kb_dir, job):
    from openkb.source_catalog import read_source, read_source_revision

    source = read_source(kb_dir, job.source_id)
    revision = read_source_revision(kb_dir, job.source_revision_id)
    if (
        source.identity != "recovered:" + job.intent_id
        or revision.source_id != job.source_id
        or revision.original != job.payload
        or revision.digest != job.digest
    ):
        raise ValueError("Pending import business result belongs to another input")
    if job.result and (
        job.result.attempt_id != job.attempt_id
        or job.result.source_id != job.source_id
        or job.result.source_revision_id != job.source_revision_id
    ):
        raise ValueError("Pending import receipt belongs to another attempt or source revision")
    return source


def reconcile_job(kb_dir, kind, job):
    """Business checkpoints win over lost receipts; unconfirmed started work never reruns."""
    if job.status == "not_imported":
        return job
    if job.status == "completed":
        if kind == "import":
            import_source(kb_dir, job)
        return job
    group = group_for(kb_dir, job)
    state = None
    if job.kb_generation != current_generation(kb_dir):
        state = "stale"
    elif kind == "import" and job.source_id:
        from openkb.ingest_records import UnitRevision
        from openkb.source_changes import source_view
        from openkb.unit_publication import list_source_units, read_unit_publication
        from openkb.workbooks.progress import workbook_progress

        source = import_source(kb_dir, job)
        if source.target_revision_id != job.source_revision_id:
            state = "stale"
        else:
            units = list_source_units(kb_dir, source.source_id)
            states = []
            for unit in units:
                try:
                    publication = read_unit_publication(
                        kb_dir, unit.unit_id, source_view(kb_dir, source)
                    )
                except FileNotFoundError:
                    continue  # Admission can commit before any unit attempt has started.
                revision = read_record(
                    kb_dir, "unit-revisions", publication.target_revision_id, UnitRevision
                )
                if revision.source_revision_id == job.source_revision_id:
                    states.append(publication.status)
            progress = workbook_progress(kb_dir, source, source_view(kb_dir, source))
            complete = len(states) == len(units)
            if progress is not None:
                complete = progress.complete
                states = [publication.status for publication in progress.publications]
            if (
                states
                and complete
                and all(value in {"completed", "empty", "retired"} for value in states)
            ):
                state = "completed"
            elif source.removed:
                state = "stopped"
            elif job.status == "started":
                state = (
                    "blocked"
                    if complete
                    and states
                    and all(
                        value
                        in {"blocked", "awaiting_confirmation", "completed", "empty", "retired"}
                        for value in states
                    )
                    else "interrupted"
                )
    if state not in {"completed", "stale"}:
        if group.cancelled or job.cancelled or group_cancelled(kb_dir, job):
            state = "cancelled"
        elif job_stopped(kb_dir, job):
            state = "stopped"
    if state is None and job.status == "started":
        state = "interrupted"
    if state and state != job.status:
        return save_job(
            kb_dir,
            kind,
            job.model_copy(
                update={
                    "status": state,
                    "message": "Unknown work requires explicit retry after business-state recovery"
                    if state == "interrupted"
                    else job.message,
                }
            ),
        )
    return job


def claim(kb_dir):
    for kind, saved in jobs(kb_dir):
        job = reconcile_job(kb_dir, kind, saved)
        if job.status not in {"pending", "dispatched"}:
            continue
        group = group_for(kb_dir, job)
        group_path = record_path(kb_dir, "execution-groups", group.root_import_id)
        if not group_path.exists():
            with mutation_scope(kb_dir, [group_path], operation="retain-execution-group"):
                write_record(group_path, group)
        job = save_job(
            kb_dir,
            kind,
            job.model_copy(update={"status": "dispatched", "dispatch_id": uuid.uuid4().hex}),
            operation="claim-pending-job",
        )
        return kind, job
    return None


def start(kb_dir, identity, dispatch_id):
    kind, job = read_job(kb_dir, identity)
    group = group_for(kb_dir, job)
    if (
        job.status != "dispatched"
        or job.dispatch_id != dispatch_id
        or group.cancelled
        or job.cancelled
        or job.kb_generation != current_generation(kb_dir)
        or group_cancelled(kb_dir, job)
        or job_stopped(kb_dir, job)
    ):
        return None
    job = save_job(
        kb_dir,
        kind,
        job.model_copy(
            update={
                "status": "started",
                "attempt_id": uuid.uuid4().hex,
                "message": None,
                **({"result": None} if kind == "import" else {}),
            }
        ),
        operation="start-pending-job",
    )
    return kind, job
