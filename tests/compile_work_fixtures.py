"""Real SQLite/CAS work ownership for isolated compiler algorithm regressions."""

import json

import pytest

from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import open_database
from ultrakb.storage.execution_store import ExecutionStore


def new_compile_work(root, object_id, conditions):
    with open_database(root, read_only=True) as db:
        identity = db.identity
    from ultrakb.application.execution import current_execution

    context = current_execution()
    if context and context.storage_identity == identity and context.storage_execution is not None:
        execution = context.storage_execution
        return CompileCheckpointStore(root, identity).for_work(
            execution.model_budget_id,
            object_id=object_id,
            conditions=conditions,
            execution_id=execution.execution_id,
        )
    execution = ExecutionStore(root, identity).begin(
        "compiler-algorithm", fingerprint="a" * 64, max_calls=None
    )
    return CompileCheckpointStore(root, identity).for_work(
        execution.model_budget_id,
        object_id=object_id,
        conditions=conditions,
        execution_id=execution.execution_id,
    )


def read_work_blob(work, digest):
    from ultrakb.storage.blobs import read_bytes

    with storage_stage(work.root, work.identity, read_only=True) as db:
        return json.loads(read_bytes(db, digest.removeprefix("blob/")))


def response_record(work, key):
    from ultrakb.storage.blobs import read_bytes

    with storage_stage(work.root, work.identity, read_only=True) as db:
        row = db.connection.execute(
            "SELECT content_digest, metadata_digest FROM ukb_compile_responses "
            "WHERE work_id = ? AND step_key = ? ORDER BY created_at DESC LIMIT 1",
            (work.work_id, key),
        ).fetchone()
        return read_bytes(db, row[0]).decode(), json.loads(read_bytes(db, row[1]))


@pytest.fixture
def compile_work(kb_dir):
    with open_database(kb_dir, read_only=True) as db:
        identity = db.identity
    executions = ExecutionStore(kb_dir, identity)
    execution = executions.begin("compiler-algorithm", fingerprint="a" * 64, max_calls=None)
    store = CompileCheckpointStore(kb_dir, identity)

    def work(object_id, conditions):
        return store.for_work(
            execution.model_budget_id,
            object_id=object_id,
            conditions=conditions,
            execution_id=execution.execution_id,
        )

    yield work
    executions.finish(execution.execution_id, state="completed", work_settled=True)


def checkpoint_record(work, key):
    with storage_stage(work.root, work.identity, read_only=True) as db:
        row = db.connection.execute(
            "SELECT record_json FROM ukb_compile_records WHERE work_id = ? AND step_key = ?",
            (work.work_id, key),
        ).fetchone()
    return json.loads(row[0]) if row else {}


def accepted_payload(work, key):
    with storage_stage(work.root, work.identity, read_only=True) as db:
        row = db.connection.execute(
            "SELECT request_digest FROM ukb_compile_steps WHERE work_id = ? AND step_key = ?",
            (work.work_id, key),
        ).fetchone()
    return work.load(key, row[0]) if row else None


def invalid_responses(work, key=None):
    from ultrakb.storage.blobs import read_bytes

    with storage_stage(work.root, work.identity, read_only=True) as db:
        rows = db.connection.execute(
            "SELECT response_id, request_digest, content_digest, metadata_digest, step_key "
            "FROM ukb_compile_responses WHERE work_id = ? "
            + ("AND step_key = ? " if key is not None else "")
            + "ORDER BY created_at, rowid",
            (work.work_id, key) if key is not None else (work.work_id,),
        ).fetchall()
        records = []
        for response_id, request, content, metadata, step_key in rows:
            record = json.loads(read_bytes(db, metadata))
            if record.get("code") and record["code"] != "response_received":
                records.append(
                    {
                        **record,
                        "raw": read_bytes(db, content),
                        "response_digest": content,
                        "request_digest": request,
                        "step_key": step_key,
                        "reference": "compile-response/" + response_id,
                    }
                )
        return records


def work_records(work):
    with storage_stage(work.root, work.identity, read_only=True) as db:
        return {
            key: json.loads(value)
            for key, value in db.connection.execute(
                "SELECT step_key, record_json FROM ukb_compile_records WHERE work_id = ?",
                (work.work_id,),
            )
        }


def accepted_step_keys(work):
    with storage_stage(work.root, work.identity, read_only=True) as db:
        return tuple(
            row[0]
            for row in db.connection.execute(
                "SELECT step_key FROM ukb_compile_steps WHERE work_id = ?",
                (work.work_id,),
            )
        )
