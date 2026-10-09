"""Execution context shared by named application operations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

from openkb.config import LlmCredentialBundle
from openkb.config_state import ConfigSnapshot, capture_config
from openkb.llm_execution import CompletionExecutor, executor_from_snapshot
from openkb.locks import LockCancelled

if TYPE_CHECKING:
    from openkb.model_task import TaskJournal


@dataclass
class ExecutionContext:
    snapshot: ConfigSnapshot | None = field(default=None, repr=False)
    cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)
    on_event: Callable[[dict], None] = field(default=lambda event: None, repr=False)
    on_snapshot: Callable[[ConfigSnapshot], None] = field(default=lambda value: None, repr=False)
    install_process_settings: bool = False
    usage_task_id: str | None = None
    executor: CompletionExecutor | None = field(default=None, repr=False)
    resume_model_task_id: str | None = None
    model_task: TaskJournal | None = field(default=None, repr=False)

    def bind_model_input(self, source_id: str, revision_id: str) -> None:
        if self.model_task is not None:
            self.model_task.bind(source_id, revision_id)

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
        from openkb.kb_format import require_current_kb

        require_current_kb(kb_dir)
        if self.snapshot is None:
            self.snapshot = capture_config(kb_dir, cancelled=self.cancelled, on_wait=self.waiting)
            # The worker's control channel acknowledges this before any
            # business work, so later batch units cannot silently re-resolve.
            self.on_snapshot(self.snapshot)
        if self.snapshot.kb_dir != str(kb_dir.resolve()):
            raise ValueError("Execution context belongs to another knowledge base")
        if self.install_process_settings:
            self.snapshot.install_worker_environment()
        if self.executor is None:
            self.executor = executor_from_snapshot(self.snapshot, self.cancelled)
        if self.model_task is None:
            from openkb.model_task import TaskJournal

            self.model_task = TaskJournal(
                kb_dir,
                self.executor,
                resume=self.resume_model_task_id,
                policy=self.snapshot.values()["effective"].get("model_policy", {}),
                execution_task_id=self.usage_task_id,
            )
        from openkb.llm_usage_execution import import_usage_execution

        with (
            import_usage_execution(
                kb_dir, "model_task", task_id=self.usage_task_id, on_event=self.on_event
            ),
            self.snapshot.activate(),
            self.executor.activate(),
        ):
            from openkb.llm_usage_execution import bind_model_task

            bind_model_task(self.model_task.record.task_id)
            self.check_stop()
            yield LlmCredentialBundle(**self.snapshot.values()["credentials"])
