"""Independent, correlated observations of newly executed work.

Request receipts remain the usage authority. These records add actual phase
timestamps without changing their strict schema or inventing historical events.
Each write is constant sized and never scans the accumulated event history.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ultrakb.llm_usage import UsageScope, active_scope, usage_scope_lifecycle
from ultrakb.locks import atomic_write_json
from ultrakb.source_records import RecordId

logger = logging.getLogger(__name__)
_CLOCK_ID = uuid.uuid4().hex
_TASK: ContextVar[str | None] = ContextVar("execution_observation_task", default=None)
Phase = Literal[
    "queued",
    "slot_acquired",
    "slot_released",
    "send",
    "response",
    "request_settled",
    "validation",
    "checkpoint_save",
    "internal_commit",
    "public_switch",
    "reuse",
    "split",
    "failure",
]


class PhaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    event_id: RecordId
    event: Phase
    execution_id: RecordId
    task_id: RecordId | None = None
    source_id: RecordId | None = None
    source_revision_id: RecordId | None = None
    unit_id: RecordId | None = None
    unit_revision_id: RecordId | None = None
    attempt_id: RecordId | None = None
    root_import_id: RecordId | None = None
    logical_call_id: RecordId | None = None
    request_attempt_id: RecordId | None = None
    stage: str = Field(min_length=1)
    work_id: str | None = None
    clock_id: str = Field(min_length=1)
    occurred_at: str
    monotonic_ns: int = Field(ge=0)
    started_at: str | None = None
    started_monotonic_ns: int | None = Field(default=None, ge=0)
    duration_ns: int | None = Field(default=None, ge=0)
    outcome: Literal["completed", "failed", "cancelled"] | None = None
    slot_limit: int | None = Field(default=None, ge=1)

    @field_validator("occurred_at", "started_at")
    @classmethod
    def utc_time(cls, value):
        if value is not None and datetime.fromisoformat(
            value
        ).utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("Phase observations require UTC timestamps")
        return value

    @model_validator(mode="after")
    def consistent_duration(self):
        if (
            self.started_at is None,
            self.started_monotonic_ns is None,
            self.duration_ns is None,
        ).count(True) not in (0, 3):
            raise ValueError("Phase duration requires a complete observed start")
        if (
            self.started_monotonic_ns is not None
            and self.duration_ns != self.monotonic_ns - self.started_monotonic_ns
        ):
            raise ValueError("Phase duration must use the process monotonic clock")
        return self


@dataclass(frozen=True)
class ObservationStart:
    """An actual in-process instant; never reconstructed from old file metadata."""

    event_id: str
    utc: str
    monotonic_ns: int
    clock_id: str


def start_observation() -> ObservationStart:
    return ObservationStart(
        uuid.uuid4().hex,
        datetime.now(timezone.utc).isoformat(),
        time.monotonic_ns(),
        f"{os.getpid()}:{_CLOCK_ID}",
    )


@contextmanager
def observation_task(task_id: str | None) -> Iterator[None]:
    token = _TASK.set(task_id or _TASK.get())
    try:
        yield
    finally:
        _TASK.reset(token)


def record_phase_event(
    event: Phase,
    *,
    stage: str,
    work_id: str | None = None,
    scope: UsageScope | None = None,
    logical_call_id: str | None = None,
    request_attempt_id: str | None = None,
    started: ObservationStart | None = None,
    outcome: Literal["completed", "failed", "cancelled"] | None = None,
    task_id: str | None = None,
    at: ObservationStart | None = None,
    event_id: str | None = None,
    slot_limit: int | None = None,
) -> str | None:
    """Retain a new observation; reporting failure cannot replay paid work."""
    scope = scope or active_scope()
    if scope is None or scope.storage is not None:
        return None
    instant = at or start_observation()
    try:
        if started is not None and started.clock_id != instant.clock_id:
            raise ValueError("A duration cannot combine clocks from different processes")
        record = PhaseEvent(
            **{
                name: getattr(scope, name)
                for name in UsageScope.__dataclass_fields__
                if name not in {"kb_dir", "on_event", "generation", "storage"}
            },
            event_id=event_id or instant.event_id,
            event=event,
            stage=stage,
            work_id=work_id,
            logical_call_id=logical_call_id,
            request_attempt_id=request_attempt_id,
            task_id=task_id or _TASK.get(),
            clock_id=instant.clock_id,
            occurred_at=instant.utc,
            monotonic_ns=instant.monotonic_ns,
            started_at=started.utc if started else None,
            started_monotonic_ns=started.monotonic_ns if started else None,
            duration_ns=instant.monotonic_ns - started.monotonic_ns if started else None,
            outcome=outcome,
            slot_limit=slot_limit,
        )
        path = (
            scope.kb_dir / ".ultrakb/observations" / scope.execution_id / f"{record.event_id}.json"
        )
        with usage_scope_lifecycle(scope):
            atomic_write_json(path, record.model_dump(mode="json"))
        return record.event_id
    except Exception as exc:
        from ultrakb.lifecycle import KnowledgeBaseRemoved

        if isinstance(exc, KnowledgeBaseRemoved):
            return None
        logger.warning("Could not retain execution phase observation", exc_info=True)
        return None


@contextmanager
def phase_observation(event: Phase, *, stage: str, **correlation) -> Iterator[str]:
    started = start_observation()
    outcome: Literal["completed", "failed", "cancelled"] = "completed"
    try:
        yield started.event_id
    except BaseException as exc:
        from ultrakb.llm_usage_transport import _failure_state

        outcome = _failure_state(exc)
        raise
    finally:
        record_phase_event(
            event,
            stage=stage,
            started=started,
            outcome=outcome,
            event_id=started.event_id,
            **correlation,
        )


def read_phase_events(kb_dir: Path, *, execution_ids=None) -> tuple[PhaseEvent, ...]:
    """Read immutable observations only on an explicit analysis request."""
    paths = kb_dir / ".ultrakb/observations"
    selected = set(execution_ids) if execution_ids is not None else None
    records = []
    for path in paths.glob("*/*.json"):
        if selected is not None and path.parent.name not in selected:
            continue
        event = PhaseEvent.model_validate_json(path.read_bytes())
        if event.event_id != path.stem or event.execution_id != path.parent.name:
            raise ValueError("Phase observation differs from its saved identity")
        records.append(event)
    return tuple(sorted(records, key=lambda row: (row.occurred_at, row.monotonic_ns, row.event_id)))


def timing_report(kb_dir: Path, **filters) -> dict:
    from ultrakb.timing_report import build_timing_report

    return build_timing_report(kb_dir, **filters)
