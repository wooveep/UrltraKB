"""Accepted compiler results outside the disposable publication workspace.

The model-task ledger owns consumption and deadlines. These records own only
accepted payloads and bounded response-repair opportunities, never a new budget.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from openkb.agent.page_response import PageResponseError
from openkb.locks import atomic_record_lock, atomic_write_json


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class CompileCheckpoints:
    def __init__(self, root: Path, task, object_id: dict, conditions: dict):
        from openkb.model_task import read_task

        self.root = root
        self.identity = object_id
        self.conditions = conditions
        self.task_id = task.task_id
        self.object_key = digest(object_id)
        self.directory = root / ".openkb/compile-checkpoints" / task.task_id / self.object_key
        self.path = self.directory / "manifest.json"
        self.ancestors = []
        seen = {task.task_id}
        while task.parent_task_id:
            if task.parent_task_id in seen:
                raise ValueError("Cyclic model-task ancestry")
            task = read_task(root, task.parent_task_id)
            seen.add(task.task_id)
            self.ancestors.append(
                root
                / ".openkb/compile-checkpoints"
                / task.task_id
                / self.object_key
                / "manifest.json"
            )

    def _read(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            record = json.loads(path.read_text("utf-8"))
            if (
                not isinstance(record, dict)
                or record.get("version") != 1
                or record.get("identity") != self.identity
                or not isinstance(record.get("entries"), dict)
            ):
                raise ValueError("Invalid checkpoint manifest")
            if record["conditions"] != self.conditions:
                return {}
            return record
        except (ValueError, KeyError) as error:
            raise PageResponseError("compile_checkpoint_invalid", str(error)) from error

    def _entry(self, key: str, request_digest: str) -> tuple[Path, dict]:
        for path in (self.path, *self.ancestors):
            entry = self._read(path).get("entries", {}).get(key)
            if entry is not None:
                if not isinstance(entry, dict):
                    raise PageResponseError("compile_checkpoint_invalid", "Invalid result entry")
                if entry.get("request_digest") == request_digest:
                    return path, entry
                break
        return self.path, {}

    def load(self, key: str, request_digest: str):
        from openkb.llm_execution import check_model_stop

        check_model_stop()
        path, entry = self._entry(key, request_digest)
        if entry.get("state") != "accepted":
            return None
        filename = f"payload-{digest(key)}.json"
        try:
            payload = json.loads((path.parent / filename).read_text("utf-8"))
            if payload.get("key") != key or digest(payload) != entry.get("payload_digest"):
                return None
            return payload["result"]
        except (OSError, ValueError, KeyError, AttributeError):
            return None

    def _write_entry(self, key: str, entry: dict):
        record = self._read(self.path) or {
            "version": 1,
            "task_id": self.task_id,
            "identity": self.identity,
            "conditions": self.conditions,
            "entries": {},
        }
        record["entries"][key] = entry
        atomic_write_json(self.path, record)

    def bind_page(self, page: str, base_sha256: str):
        """A resumed update cannot reinterpret an externally changed page as its base."""
        from openkb.llm_execution import check_model_stop

        key = f"base:{page}"
        with atomic_record_lock(self.directory / "checkpoint.lock"):
            check_model_stop()
            for path in (self.path, *self.ancestors):
                previous = self._read(path).get("entries", {}).get(key)
                if previous and previous.get("base_sha256") != base_sha256:
                    raise PageResponseError("page_update_stale", "Retained update base changed")
            self._write_entry(key, {"state": "base", "base_sha256": base_sha256})

    def save(self, key: str, request_digest: str, result, *, metadata: dict | None = None):
        from openkb.llm_execution import check_model_stop

        with atomic_record_lock(self.directory / "checkpoint.lock"):
            check_model_stop()
            _, previous = self._entry(key, request_digest)
            payload = {"key": key, "result": result}
            atomic_write_json(self.directory / f"payload-{digest(key)}.json", payload)
            check_model_stop()
            self._write_entry(
                key,
                {
                    **previous,
                    "request_digest": request_digest,
                    "state": "accepted",
                    "payload_digest": digest(payload),
                    "metadata": metadata or {},
                },
            )
        from openkb.compilation_report import report_compile_checkpoint

        report_compile_checkpoint(str(self.path.relative_to(self.root)), key, "accepted")

    def require_generation(self, key: str, request_digest: str):
        _, entry = self._entry(key, request_digest)
        if entry.get("repair_used"):
            raise PageResponseError(
                "compile_repair_exhausted", "The retained response repair was already consumed"
            )

    def claim_repair(self, key: str, request_digest: str):
        from openkb.llm_execution import check_model_stop

        with atomic_record_lock(self.directory / "checkpoint.lock"):
            check_model_stop()
            self.require_generation(key, request_digest)
            _, previous = self._entry(key, request_digest)
            self._write_entry(
                key,
                {
                    **previous,
                    "request_digest": request_digest,
                    "state": "repairing",
                    "repair_used": True,
                },
            )

    def claim_conflict(self, key: str, request_digest: str):
        from openkb.llm_execution import check_model_stop

        with atomic_record_lock(self.directory / "checkpoint.lock"):
            check_model_stop()
            _, previous = self._entry(key, request_digest)
            if previous.get("conflict_used"):
                raise PageResponseError(
                    "compile_conflict_exhausted", "The retained conflict attempt was consumed"
                )
            self._write_entry(
                key,
                {
                    **previous,
                    "request_digest": request_digest,
                    "state": "resolving",
                    "conflict_used": True,
                },
            )


def document_checkpoints(kb_dir: Path | None, view_id: str, doc_name: str):
    from openkb.application.execution import current_execution

    context = current_execution()
    if kb_dir is None or context is None or context.model_task is None:
        return None
    journal = context.model_task
    if journal.root.resolve() != kb_dir.resolve():
        raise ValueError("Checkpoint belongs to another knowledge base")
    executor = journal.executor
    credentials = executor.bindings.credentials()
    # Only processing identities for this document participate; adding another
    # source to the same batch must not invalidate already accepted results.
    from openkb.ingest_records import ImportUnit, UnitRevision
    from openkb.source_catalog import read_record

    revisions = []
    for path in (kb_dir / ".openkb/catalog/units").glob("*.json"):
        unit = read_record(kb_dir, "units", path.stem, ImportUnit)
        if unit.doc_name == doc_name:
            revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
            revisions.append({"unit": unit.model_dump(), "revision": revision.model_dump()})
    return CompileCheckpoints(
        kb_dir,
        journal.record,
        {"kb_generation": journal.record.kb_generation, "view_id": view_id, "document": doc_name},
        {
            "compiler_protocol": "accepted-results-v1",
            "bindings": executor.bindings.public_identity(),
            "endpoint_digest": digest(credentials.base_url),
            "revisions": revisions,
        },
    )


def cleanup_published_checkpoints(kb_dir: Path, view_id: str, doc_name: str) -> None:
    """Publication is already committed; cleanup failure cannot roll it back."""
    import logging
    import shutil

    try:
        checkpoints = document_checkpoints(kb_dir, view_id, doc_name)
        if checkpoints:
            for manifest in (checkpoints.path, *checkpoints.ancestors):
                if manifest.exists():
                    shutil.rmtree(manifest.parent)
    except (OSError, ValueError) as error:
        logging.getLogger(__name__).warning(
            "Published compile checkpoint cleanup deferred: %s", error
        )
