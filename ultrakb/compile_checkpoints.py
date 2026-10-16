"""Accepted compiler results outside the disposable publication workspace.

The model-task ledger owns consumption and deadlines. These records own only
accepted payloads and bounded response-repair opportunities, never a new budget.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ultrakb.agent.page_response import PageResponseError
from ultrakb.locks import atomic_record_lock, atomic_write_json, atomic_write_text

_FIGURE_BASES: ContextVar[dict[Path, tuple[str, str]]] = ContextVar("figure_bases", default={})


@contextmanager
def figure_projection_bases(wiki: Path, original_pages: dict[str, str]):
    """Bind the exact display-only projection to its unchanged pre-repair base."""
    from ultrakb.state import HashRegistry

    projected = {}
    for relative, before in original_pages.items():
        page = wiki / relative
        if page.suffix == ".md" and page.is_file():
            after = HashRegistry.hash_file(page)
            if before != after:
                projected[page.resolve()] = (before, after)
    token = _FIGURE_BASES.set(projected)
    try:
        yield
    finally:
        _FIGURE_BASES.reset(token)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class CompileCheckpoints:
    def __init__(self, root: Path, task, object_id: dict, conditions: dict):
        from ultrakb.model_task import read_task

        self.root = root
        self.identity = object_id
        self.conditions = conditions
        self.task_id = task.task_id
        self.request_version = getattr(task, "compiler_request_version", 1)
        self.object_key = digest(object_id)
        self.directory = root / ".ultrakb/compile-checkpoints" / task.task_id / self.object_key
        self.failure_directory = (
            root / ".ultrakb/compile-responses" / task.task_id / self.object_key
        )
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
                / ".ultrakb/compile-checkpoints"
                / task.task_id
                / self.object_key
                / "manifest.json"
            )

    def _read(self, path: Path, *, match_conditions: bool = True) -> dict:
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
            if not isinstance(record.get("conditions"), dict):
                raise ValueError("Invalid checkpoint conditions")
            if match_conditions and record["conditions"] != self.conditions:
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

    def require_request(self, key: str, request_digest: str) -> None:
        """An old frozen request cannot silently become fresh work after an upgrade."""
        if self.request_version != 1:
            return
        for path in (self.path, *self.ancestors):
            entry = self._read(path).get("entries", {}).get(key)
            if entry is not None:
                if not isinstance(entry, dict):
                    raise PageResponseError("compile_checkpoint_invalid", "Invalid result entry")
                if entry.get("request_digest") != request_digest:
                    raise PageResponseError(
                        "compile_request_changed",
                        f"Cannot reconstruct historical request {key}; saved digest "
                        f"{entry.get('request_digest')} differs from {request_digest}. "
                        "Original responses and allowances are retained.",
                    )
                return

    @property
    def legacy_summary_format(self) -> bool:
        """Retained pre-contract windows keep their original JSON-mode admission."""
        for path in (self.path, *self.ancestors):
            entries = self._read(path).get("entries", {})
            if entries.get("protocol:summary", {}).get("version") in {1, 2}:
                return False
            if any(
                key == "short-summary" or key.startswith(("summary:", "summary-split:"))
                for key in entries
            ):
                return True
        return False

    @property
    def structured_summary(self) -> bool:
        """Old request bytes and accepted results are never migrated implicitly."""
        for path in (self.path, *self.ancestors):
            entries = self._read(path).get("entries", {})
            version = entries.get("protocol:summary", {}).get("version")
            if version is not None:
                return version == 2
            if any(
                key == "short-summary" or key.startswith(("summary:", "summary-split:"))
                for key in entries
            ):
                return False
        return True

    def prepare_summary_delivery(self) -> bool:
        from ultrakb.llm_execution import check_model_stop

        with atomic_record_lock(self.directory / "checkpoint.lock"):
            check_model_stop()
            if self.legacy_summary_format:
                return True
            entries = self._read(self.path).get("entries", {})
            if "protocol:summary" not in entries:
                self._write_entry(
                    "protocol:summary", {"version": 2 if self.structured_summary else 1}
                )
        return False

    def load(self, key: str, request_digest: str):
        from ultrakb.llm_execution import check_model_stop

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
        record = self._read(self.path, match_conditions=False) or {
            "version": 1,
            "task_id": self.task_id,
            "identity": self.identity,
            "conditions": self.conditions,
            "entries": {},
        }
        if record["conditions"] != self.conditions:
            # Protocol changes invalidate generated payloads, never page identity.
            record["entries"] = {
                key: value for key, value in record["entries"].items() if key.startswith("base:")
            }
            record["conditions"] = self.conditions
        record["entries"][key] = entry
        atomic_write_json(self.path, record)

    def bind_page(self, page: str, base_sha256: str, *, page_path: Path | None = None):
        """A resumed update cannot reinterpret an externally changed page as its base."""
        from ultrakb.llm_execution import check_model_stop

        projection = _FIGURE_BASES.get().get(page_path.resolve()) if page_path else None
        allowed_bases = {base_sha256}
        if projection and base_sha256 == projection[1]:
            allowed_bases.add(projection[0])
            base_sha256 = projection[0]
        key = f"base:{page}"
        with atomic_record_lock(self.directory / "checkpoint.lock"):
            check_model_stop()
            for path in (self.path, *self.ancestors):
                previous = self._read(path, match_conditions=False).get("entries", {}).get(key)
                if previous is not None and not isinstance(previous, dict):
                    raise PageResponseError("compile_checkpoint_invalid", "Invalid page base")
                if previous and previous.get("base_sha256") not in allowed_bases:
                    raise PageResponseError("page_update_stale", "Retained update base changed")
            self._write_entry(key, {"state": "base", "base_sha256": base_sha256})

    def save(
        self,
        key: str,
        request_digest: str,
        result,
        *,
        metadata: dict | None = None,
        reused_response: dict | None = None,
    ):
        from ultrakb.agent.compile_contract import ACCEPTANCE_VERSION
        from ultrakb.llm_execution import check_model_stop

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
                    "acceptance_version": ACCEPTANCE_VERSION,
                    "payload_digest": digest(payload),
                    "metadata": metadata or {},
                    **({"reused_response": reused_response} if reused_response else {}),
                },
            )
        from ultrakb.compilation_report import report_compile_checkpoint

        report_compile_checkpoint(str(self.path.relative_to(self.root)), key, "accepted")

    def require_generation(self, key: str, request_digest: str):
        _, entry = self._entry(key, request_digest)
        if entry.get("repair_used"):
            raise PageResponseError(
                "compile_repair_exhausted", "The retained response repair was already consumed"
            )

    def claim_generation(self, key: str, request_digest: str, *, stage: str):
        """Before a send, consume at most one explicit recovery of an unknown repair."""
        from ultrakb.compile_recovery import failed_summary_request, interrupted_repair_request
        from ultrakb.llm_execution import check_model_stop

        with atomic_record_lock(self.directory / "checkpoint.lock"):
            check_model_stop()
            path, entry = self._entry(key, request_digest)
            if not entry.get("repair_used"):
                return
            request = None
            if (
                path == self.path
                and entry.get("state") == "repairing"
                and entry.get("repair_used") is True
                and entry.get("failure_code") is None
                and not entry.get("recovery_used")
            ):
                request = interrupted_repair_request(self.root, self.task_id, stage)
            if request is None:
                manual = None
                if (
                    path == self.path
                    and entry.get("state") in {"repairing", "resuming"}
                    and not entry.get("manual_retry_used")
                ):
                    manual = failed_summary_request(self.root, self.task_id, stage)
                if manual is None:
                    self.require_generation(key, request_digest)
                    return
                check_model_stop()
                self._write_entry(
                    key,
                    {
                        **entry,
                        "state": "resuming",
                        "manual_retry_used": True,
                        "manual_retry": {**manual, "request_digest": request_digest},
                    },
                )
                return
            check_model_stop()
            self._write_entry(
                key,
                {
                    **entry,
                    "state": "resuming",
                    "recovery_used": True,
                    "recovery": {**request, "request_digest": request_digest},
                },
            )

    def failure_code(self, key: str, request_digest: str) -> str | None:
        _, entry = self._entry(key, request_digest)
        return entry.get("failure_code")

    def retained_response(
        self, key: str, request_digest: str, *, protocol: str, sections: tuple[str, ...], stage: str
    ) -> tuple[str, dict] | None:
        """Read only the latest captured response for these exact frozen inputs."""
        path, entry = self._entry(key, request_digest)
        failures = entry.get("response_failures", ())
        candidate = entry.get("latest_response") or (failures[-1] if failures else None)
        if candidate is None:
            return None
        try:
            owner = path.parent.parent.name
            directory = (
                self.root / ".ultrakb/compile-responses" / owner / self.object_key
            ).resolve()
            if not directory.is_relative_to(self.root.resolve()):
                raise ValueError("Saved response directory escapes the knowledge base")
            record_path = (self.root / candidate).resolve()
            if record_path.parent != directory or record_path.suffix != ".json":
                raise ValueError("Saved response path differs")
            record = json.loads(record_path.read_text("utf-8"))
            if not isinstance(record, dict) or any(
                record.get(name) != expected
                for name, expected in {
                    "version": 1,
                    "task_id": owner,
                    "checkpoint_key": key,
                    "request_digest": request_digest,
                    "stage": stage,
                    "response_protocol": protocol,
                    "expected_sections": list(sections),
                }.items()
            ):
                raise ValueError("Saved response identity differs")
            if record.get("error_code") == "page_truncated_response":
                return None
            response_path = (self.root / record["response_file"]).resolve()
            if response_path != record_path.with_suffix(".txt"):
                raise ValueError("Saved response body path differs")
            content = response_path.read_bytes()
            if hashlib.sha256(content).hexdigest() != record["response_sha256"]:
                raise ValueError("Saved response checksum differs")
            return content.decode("utf-8"), {
                "record_file": str(record_path.relative_to(self.root)),
                "response_file": str(response_path.relative_to(self.root)),
                "response_sha256": record["response_sha256"],
                "execution_id": record.get("execution_id"),
                "awaiting_validation": record.get("error_code") == "response_received",
            }
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise PageResponseError("compile_checkpoint_invalid", str(error)) from error

    def record_invalid_response(
        self,
        key: str,
        request_digest: str,
        response: str,
        *,
        stage: str,
        response_protocol: str,
        expected_sections: tuple[str, ...],
        code: str,
        detail: str,
        attempt: int,
        field_errors: tuple[dict, ...] = (),
        response_format: dict | None = None,
        received: bool = False,
    ) -> str:
        """Retain untrusted delivery bytes without granting acceptance or another repair."""
        from datetime import datetime, timezone
        from uuid import uuid4

        from ultrakb.llm_usage import active_scope

        scope = active_scope()
        filename = self.failure_directory / uuid4().hex
        response_file = filename.with_suffix(".txt")
        diagnostic_file = filename.with_suffix(".json")
        record = {
            "version": 1,
            "task_id": self.task_id,
            "execution_id": scope.execution_id if scope else None,
            "checkpoint_key": key,
            "request_digest": request_digest,
            "stage": stage,
            "response_protocol": response_protocol,
            "expected_sections": list(expected_sections),
            "error_code": code,
            "error_detail": detail,
            "field_errors": list(field_errors),
            "response_format": response_format,
            "attempt": attempt,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "response_file": str(response_file.relative_to(self.root)),
            "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
        }
        with atomic_record_lock(self.directory / "checkpoint.lock"):
            _, previous = self._entry(key, request_digest)
            # Classify the already persisted response in place after validation;
            # one provider response owns one immutable body file.
            if not received and previous.get("latest_response"):
                saved = self.root / previous["latest_response"]
                if saved.parent == self.failure_directory:
                    captured = json.loads(saved.read_text())
                    if (
                        captured.get("error_code") == "response_received"
                        and captured.get("response_sha256") == record["response_sha256"]
                        and captured.get("request_digest") == request_digest
                        and captured.get("attempt") == attempt
                    ):
                        diagnostic_file, response_file = saved, saved.with_suffix(".txt")
                        record["response_file"] = str(response_file.relative_to(self.root))
            atomic_write_text(response_file, response)
            atomic_write_json(diagnostic_file, record)
            self._write_entry(
                key,
                {
                    **previous,
                    "request_digest": request_digest,
                    "latest_response": str(diagnostic_file.relative_to(self.root)),
                    ("received_responses" if received else "response_failures"): [
                        *previous.get(
                            "received_responses" if received else "response_failures", ()
                        ),
                        str(diagnostic_file.relative_to(self.root)),
                    ],
                },
            )
        return str(response_file.relative_to(self.root))

    def record_received_response(self, key, request_digest, response, **metadata):
        """Persist received bytes before validation, without accepting them or renewing repair."""
        return self.record_invalid_response(
            key,
            request_digest,
            response,
            received=True,
            detail="Received; awaiting source validation",
            **metadata,
        )

    def record_delivery_resolution(self, key, request_digest, raw, delivered, resolutions):
        """Keep a receipt for deterministic copies without consuming a model send."""
        from datetime import datetime, timezone
        from uuid import uuid4

        filename = self.failure_directory / f"resolved-{uuid4().hex}.json"
        record = {
            "version": 1,
            "task_id": self.task_id,
            "checkpoint_key": key,
            "request_digest": request_digest,
            "policy": "source-resolution-v1",
            "response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "delivered_sha256": hashlib.sha256(delivered.encode()).hexdigest(),
            "resolutions": resolutions,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        with atomic_record_lock(self.directory / "checkpoint.lock"):
            _, previous = self._entry(key, request_digest)
            atomic_write_json(filename, record)
            self._write_entry(
                key,
                {
                    **previous,
                    "request_digest": request_digest,
                    "delivery_resolutions": [
                        *previous.get("delivery_resolutions", ()),
                        str(filename.relative_to(self.root)),
                    ],
                },
            )
        return str(filename.relative_to(self.root))

    def record_failure(self, key: str, request_digest: str, code: str):
        """Persist an observed response outcome even when cancellation is pending.

        This consumes no send or repair opportunity. In particular, a stopped
        summary can recover its length failure without repeating the parent.
        """
        with atomic_record_lock(self.directory / "checkpoint.lock"):
            _, previous = self._entry(key, request_digest)
            self._write_entry(
                key,
                {**previous, "request_digest": request_digest, "failure_code": code},
            )

    def claim_repair(self, key: str, request_digest: str):
        from ultrakb.llm_execution import check_model_stop

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
        from ultrakb.llm_execution import check_model_stop

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
    from ultrakb.application.execution import current_execution

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
    from ultrakb.ingest_records import ImportUnit, UnitRevision
    from ultrakb.source_catalog import read_record

    revisions = []
    for path in (kb_dir / ".ultrakb/catalog/units").glob("*.json"):
        unit = read_record(kb_dir, "units", path.stem, ImportUnit)
        if unit.doc_name == doc_name:
            revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
            revisions.append({"unit": unit.model_dump(), "revision": revision.model_dump()})
    return CompileCheckpoints(
        kb_dir,
        journal.record,
        {"kb_generation": journal.record.kb_generation, "view_id": view_id, "document": doc_name},
        {
            # Recompute pre-figure results while keeping page bases and task usage.
            "compiler_protocol": "accepted-results-text-figures-v2",
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
