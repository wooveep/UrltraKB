"""Durable model-task budgets; restart does not reset paid work or its deadline."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from pathlib import Path

from pydantic import Field, TypeAdapter

from ultrakb.file_state import contained_paths
from ultrakb.lifecycle import current_generation
from ultrakb.locks import atomic_record_lock, atomic_write_json
from ultrakb.source_records import Record, RecordId


class ModelTask(Record):
    task_id: RecordId
    parent_task_id: RecordId | None = None
    kb_generation: str
    fingerprint: str
    bindings: dict
    policy: dict
    deadline_at: float | None = None
    used: int = Field(default=0, ge=0)
    stages: dict[str, int] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    execution_task_ids: list[RecordId] = Field(default_factory=list)


def task_path(kb_dir: Path, task_id: str) -> Path:
    TypeAdapter(RecordId).validate_python(task_id)
    path = kb_dir / ".ultrakb/model-tasks" / f"{task_id}.json"
    contained_paths(kb_dir, [path])
    return path


def read_task(kb_dir: Path, task_id: str) -> ModelTask:
    value = ModelTask.model_validate_json(task_path(kb_dir, task_id).read_text("utf-8"))
    if value.task_id != task_id or value.kb_generation != current_generation(kb_dir):
        raise ValueError("Model task belongs to another knowledge base generation")
    return value


class TaskJournal:
    def __init__(
        self,
        kb_dir,
        executor,
        *,
        resume=None,
        policy=None,
        execution_task_id=None,
        parent_execution_task_id=None,
    ):
        self.root, self.executor = kb_dir, executor
        policy = dict(policy or {})
        policy.setdefault("max_calls", executor.policy.max_calls)
        if execution_task_id:
            resume = execution_task(kb_dir, execution_task_id) or resume
        if not resume and parent_execution_task_id:
            resume = execution_task(kb_dir, parent_execution_task_id)
        previous = read_task(kb_dir, resume) if resume else None
        bindings = executor.bindings.public_identity()
        deadline = executor.policy.deadline
        deadline_at = time.time() + deadline - time.monotonic() if deadline is not None else None
        self.record = ModelTask(
            task_id=uuid.uuid4().hex,
            parent_task_id=resume,
            kb_generation=current_generation(kb_dir),
            fingerprint=executor.fingerprint,
            bindings=bindings,
            policy=policy,
            deadline_at=deadline_at,
        )
        if previous:
            # Only an explicitly changed captured limit may expand this logical task.
            before, after = dict(previous.policy), dict(policy)
            old_limit, new_limit = before.pop("max_calls", 1000), after.pop("max_calls", 1000)
            if previous.bindings != bindings or before != after or new_limit < old_limit:
                raise ValueError("Task policy changed; start a new task or reprocess the source")
            self.record = (
                previous
                if previous.fingerprint == executor.fingerprint
                else self.record.model_copy(
                    update={
                        "used": previous.used,
                        "stages": previous.stages,
                        "inputs": previous.inputs,
                        "deadline_at": previous.deadline_at,
                        "execution_task_ids": previous.execution_task_ids,
                    }
                )
            )
            if self.record.deadline_at is not None:
                executor.policy = replace(
                    executor.policy,
                    deadline=time.monotonic() + self.record.deadline_at - time.time(),
                )
                executor.budget.policy = executor.policy
            executor.budget.restore(self.record.used, self.record.stages)
        if execution_task_id and execution_task_id not in self.record.execution_task_ids:
            TypeAdapter(RecordId).validate_python(execution_task_id)
            self.record = self.record.model_copy(
                update={"execution_task_ids": [*self.record.execution_task_ids, execution_task_id]}
            )
        self.save(executor.budget.snapshot())
        executor.budget.on_change = self.save

    def save(self, budget):
        with atomic_record_lock(self.root / ".ultrakb/model-tasks/ledger.lock"):
            self.record = self.record.model_copy(
                update={"used": budget["used"], "stages": budget["stages"]}
            )
            atomic_write_json(task_path(self.root, self.record.task_id), self.record.model_dump())

    def bind(self, source_id: str, revision_id: str):
        TypeAdapter(RecordId).validate_python(source_id)
        TypeAdapter(RecordId).validate_python(revision_id)
        self.record = self.record.model_copy(
            update={"inputs": {**self.record.inputs, source_id: revision_id}}
        )
        self.save(self.executor.budget.snapshot())


def execution_task(kb_dir, execution_task_id):
    """Resume successive spawned units while holding their shared KB lease."""
    candidates = []
    for path in (kb_dir / ".ultrakb/model-tasks").glob("*.json"):
        record = read_task(kb_dir, path.stem)
        if execution_task_id in record.execution_task_ids:
            candidates.append((path.stat().st_mtime_ns, record.task_id))
    return max(candidates)[1] if candidates else None


def source_task(kb_dir, source_id, revision_id):
    """Select the latest current-revision task while the application holds its lease."""
    candidates = []
    for path in (kb_dir / ".ultrakb/model-tasks").glob("*.json"):
        record = read_task(kb_dir, path.stem)
        if record.inputs.get(source_id) == revision_id:
            candidates.append((path.stat().st_mtime_ns, record.task_id))
    return max(candidates)[1] if candidates else None
