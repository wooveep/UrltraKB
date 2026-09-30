"""Atomic cancellation signals can reach a worker while it owns the KB write lease."""

import json

from pydantic import TypeAdapter

from openkb.catalog_schema import validate_catalog_writer
from openkb.file_state import contained_paths
from openkb.lifecycle import current_generation, expected_generation, read_lifecycle
from openkb.locks import atomic_write_json
from openkb.source_records import RecordId


def _path(kb_dir, group_id=None, job_id=None, dispatch_id=None):
    for value in (group_id, job_id, dispatch_id):
        if value is not None:
            TypeAdapter(RecordId).validate_python(value)
    name = "group-" + group_id if group_id else f"job-{job_id}-{dispatch_id}"
    path = kb_dir / ".openkb/pending-control" / (name + ".json")
    contained_paths(kb_dir, [path])
    return path


def request_stop(kb_dir, job_id, dispatch_id, generation):
    with expected_generation(kb_dir, generation), read_lifecycle(kb_dir):
        validate_catalog_writer(kb_dir)
        atomic_write_json(
            _path(kb_dir, job_id=job_id, dispatch_id=dispatch_id),
            {
                "generation": current_generation(kb_dir),
                "job_id": job_id,
                "dispatch_id": dispatch_id,
            },
        )


def request_group_cancel(kb_dir, group):
    with expected_generation(kb_dir, group.kb_generation), read_lifecycle(kb_dir):
        validate_catalog_writer(kb_dir)
        atomic_write_json(
            _path(kb_dir, group_id=group.root_import_id),
            {
                "generation": group.kb_generation,
                "root_import_id": group.root_import_id,
            },
        )


def _requested(path, expected):
    try:
        value = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return False
    if value != expected:
        raise ValueError("Pending cancellation control record is inconsistent")
    return True


def group_cancelled(kb_dir, job):
    return _requested(
        _path(kb_dir, group_id=job.root_import_id),
        {
            "generation": job.kb_generation,
            "root_import_id": job.root_import_id,
        },
    )


def job_stopped(kb_dir, job):
    return bool(job.dispatch_id) and _requested(
        _path(kb_dir, job_id=job.intent_id, dispatch_id=job.dispatch_id),
        {"generation": job.kb_generation, "job_id": job.intent_id, "dispatch_id": job.dispatch_id},
    )
