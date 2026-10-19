"""Execution context shared by named application operations."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal
from uuid import uuid4

from ultrakb.config import LlmCredentialBundle
from ultrakb.config_state import ConfigSnapshot
from ultrakb.llm_execution import CompletionExecutor
from ultrakb.locks import LockCancelled

if TYPE_CHECKING:
    from ultrakb.storage.database import DatabaseIdentity
    from ultrakb.storage.execution_store import Execution


@dataclass
class ExecutionContext:
    snapshot: ConfigSnapshot | None = field(default=None, repr=False)
    cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)
    on_event: Callable[[dict], None] = field(default=lambda event: None, repr=False)
    on_snapshot: Callable[[ConfigSnapshot], None] = field(default=lambda value: None, repr=False)
    install_process_settings: bool = False
    usage_task_id: str | None = None
    usage_unit_id: str | None = None
    observe_storage_costs: bool = True
    parent_usage_task_id: str | None = None
    require_verified_connection: bool = False
    executor: CompletionExecutor | None = field(default=None, repr=False)
    storage_identity: DatabaseIdentity | None = field(default=None, repr=False)
    credential_bundle: LlmCredentialBundle | None = field(default=None, repr=False)
    resume_storage_execution_id: str | None = None
    storage_pending_attempt_id: str | None = None
    storage_execution: Execution | None = field(default=None, init=False)
    _storage_running: bool = field(default=False, init=False, repr=False)
    _storage_result: Literal["completed", "failed", "cancelled"] = field(
        default="completed", init=False, repr=False
    )
    activity_attempt_id: str = field(default_factory=lambda: uuid4().hex)
    _activity_task_id: str = field(default_factory=lambda: uuid4().hex, repr=False)
    _event_guard: RLock = field(default_factory=RLock, repr=False)
    _read_sources: set[str] = field(default_factory=set, repr=False)
    _read_units: set[tuple] = field(default_factory=set, repr=False)
    returned_reads: list[dict] = field(default_factory=list, repr=False)
    tool_activities: dict[str, dict] = field(default_factory=dict, repr=False)

    def reset_reads(self):
        with self._event_guard:
            self._read_sources.clear()
            self._read_units.clear()
            self.returned_reads.clear()

    def record_reads(self, payload: dict) -> None:
        """Count only original content actually returned, without disclosing names."""
        with self._event_guard:
            hits = payload.get("hits", [payload])
            for hit in hits if isinstance(hits, list) else []:
                if not isinstance(hit, dict):
                    continue
                if not hit.get("content") or hit.get("content_kind") != "original_source":
                    continue
                source = hit.get("source_context", {})
                if not isinstance(source, dict):
                    continue
                identity = source.get("content_digest") or hit.get("content_digest")
                if not identity:
                    continue
                path = source.get("source_path", hit.get("path"))
                if isinstance(path, str):
                    self.returned_reads.append(
                        {
                            "path": path,
                            "digest": identity,
                            "source_revision_id": source.get("source_revision_id"),
                            "location": source.get("native_location", {}),
                        }
                    )
                identity = hashlib.sha256(
                    json.dumps(
                        [
                            identity,
                            source.get("source_revision_id"),
                            source.get("source_path", hit.get("path")),
                        ]
                    ).encode()
                ).hexdigest()
                self._read_sources.add(identity)
                units: list[tuple] = (
                    [("page", p["page"]) for p in hit.get("page_spans", [])]
                    or [("block", b["ordinal"]) for b in hit.get("blocks", [])]
                    or [("cell", c["coordinate"]) for c in hit.get("cells", [])]
                    or [("text",)]
                )
                self._read_units.update((identity, *unit) for unit in units)

    @contextmanager
    def activity(self, stage: str, *, tool_call_id: str | None = None) -> Iterator[dict]:
        parent = _PARENT_ACTIVITY.get()
        data: dict[str, Any] = {
            "task_id": self.usage_task_id or self._activity_task_id,
            "attempt_id": self.activity_attempt_id,
            "activity_id": uuid4().hex,
            "parent_activity_id": parent,
            "stage": stage,
            "status": "started",
        }
        if tool_call_id:
            data = {**self.tool_metadata(tool_call_id, stage), "status": "started"}
        began = monotonic()
        with self._event_guard:
            self.on_event({"event": "activity", "data": dict(data)})
        _PARENT_ACTIVITY.set(data["activity_id"])
        try:
            yield data
            if data["status"] == "started":
                data["status"] = "completed"
        except BaseException as error:
            data["status"] = (
                "cancelled"
                if isinstance(error, (LockCancelled, GeneratorExit))
                or type(error).__name__ == "CancelledError"
                else "failed"
            )
            data["reason_code"] = type(error).__name__
            raise
        finally:
            _PARENT_ACTIVITY.set(parent)
            with self._event_guard:
                data.update(
                    elapsed_seconds=round(monotonic() - began, 3),
                    source_count=len(self._read_sources),
                    unit_count=len(self._read_units),
                )
                if tool_call_id:
                    self.tool_activities[tool_call_id] = dict(data)
                self.on_event({"event": "activity", "data": dict(data)})

    def tool_metadata(self, call_id: str, stage: str) -> dict:
        with self._event_guard:
            return dict(
                self.tool_activities.setdefault(
                    call_id,
                    {
                        "task_id": self.usage_task_id or self._activity_task_id,
                        "attempt_id": self.activity_attempt_id,
                        "activity_id": uuid4().hex,
                        "parent_activity_id": _PARENT_ACTIVITY.get(),
                        "stage": stage,
                    },
                )
            )

    def check_stop(self) -> None:
        if self.cancelled():
            raise LockCancelled("Stopped before starting the next operation")
        if self.executor is not None:
            self.executor.policy.check()

    def waiting(self) -> None:
        self.on_event({"stage": "waiting"})

    @contextmanager
    def begin(self, kb_dir: Path) -> Iterator[LlmCredentialBundle]:
        self.check_stop()
        if self.storage_identity is None:
            from ultrakb.storage.database import open_database

            with open_database(kb_dir, read_only=True) as db:
                self.storage_identity = db.identity
        from .storage_execution import begin_storage_execution

        with begin_storage_execution(kb_dir, self) as bundle:
            yield bundle
        return


_ACTIVE_CONTEXT: ContextVar[ExecutionContext | None] = ContextVar("ultrakb_execution", default=None)
_PARENT_ACTIVITY: ContextVar[str | None] = ContextVar("ultrakb_parent_activity", default=None)


@contextmanager
def _activate_context(context):
    token = _ACTIVE_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTIVE_CONTEXT.reset(token)


def current_execution() -> ExecutionContext | None:
    return _ACTIVE_CONTEXT.get()


@contextmanager
def activity(stage: str, *, tool_call_id: str | None = None):
    context = current_execution()
    if context is None:
        yield {}
    else:
        with context.activity(stage, tool_call_id=tool_call_id) as data:
            yield data
