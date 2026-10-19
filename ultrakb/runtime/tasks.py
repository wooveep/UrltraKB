"""Bounded spawn scheduling independent of windows and progress observers."""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import queue
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ultrakb.config_state import ConfigSnapshot
from ultrakb.locks import atomic_write_json
from ultrakb.runtime.input_store import InputStore
from ultrakb.runtime.records import (
    TERMINAL,
    PendingStop,
    TaskExclusion,
    TaskView,
    UnitIdentity,
    UnitResult,
    read_receipt,
)
from ultrakb.runtime.requests import REQUEST_TYPES, RecompileDocument, UnitRequest
from ultrakb.runtime.worker import run_unit


@dataclass
class _Task:
    view: TaskView
    requests: tuple[UnitRequest, ...] = field(default=(), repr=False)
    identities: tuple[UnitIdentity, ...] = ()
    snapshot: ConfigSnapshot | None = field(default=None, repr=False)
    ready_at: float = 0.0
    wait_delay: float = 0.25
    require_verified_connection: bool = False
    config_location: tuple[Path, Path] | None = None


@dataclass
class _Attempt:
    process: Any
    control: Any
    events: Any
    identity: UnitIdentity
    result: UnitResult | None = None
    deferred: bool = False
    deferred_reason: str = "lease"
    unconfirmed: bool = False
    stop_sent: bool = False
    eof: bool = False
    last_sequence: int = 0
    terminal_sequence: int | None = None


class TaskManager:
    """Submit explicit units, observe immutable views, and stop at safe boundaries.

    Each unit gets a fresh spawn child. A batch yields between units and keeps
    its acknowledged configuration only in memory. History never starts work.
    ``wait``/``join`` are for non-UI callers; Qt observes ``get``/``tasks``.
    """

    def __init__(
        self, *, history_dir: Path, max_workers: int = 2, require_verified_connection: bool = False
    ) -> None:
        if not 1 <= max_workers <= 8:
            raise ValueError("Worker limit must be between 1 and 8")
        self.history_dir = history_dir.expanduser().resolve()
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.receipt_dir = self.history_dir / "units"
        self._preparations = InputStore(self.history_dir)
        self.max_workers = max_workers
        self.require_verified_connection = require_verified_connection
        self._context = mp.get_context("spawn")
        self._condition = threading.Condition(threading.RLock())
        self._tasks: dict[str, _Task] = {}
        self._pending: deque[str] = deque()
        self._active: dict[str, _Attempt] = {}
        self._clearing: set[str] = set()
        self._stopping_controls: set[str] = set()
        self._accepting = True
        self._closing = False
        self._load_history()
        self._thread = threading.Thread(target=self._run, name="ultrakb-tasks", daemon=False)
        self._thread.start()

    def _load_history(self) -> None:
        for path in self.history_dir.glob("*.json"):
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                view = TaskView.from_summary(saved["view"])
                identities = tuple(UnitIdentity(**item) for item in saved["identities"])
                # Recover lost delivery from correlated receipts, without
                # replaying a request or guessing from artifact existence.
                results = list(view.results)
                for identity in identities[len(results) :]:
                    result = read_receipt(self.receipt_dir, identity)
                    if result is None:
                        break
                    results.append(result)
                view = replace(view, results=tuple(results))
                from ultrakb.llm_usage_execution import task_usage_receipt

                view = replace(
                    view,
                    model_usage=task_usage_receipt(Path(view.kb_dir), view.id) or view.model_usage,
                )
                if view.state == "interrupted" and len(results) == view.total:
                    view = replace(
                        view,
                        stage="results-confirmed",
                        error="Business results confirmed; previous process cleanup is unverified",
                    )
                self._tasks[view.id] = _Task(view, identities=identities)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                # Keep damaged evidence on disk; it must not become executable.
                continue

    def _persist(self, task: _Task) -> None:
        atomic_write_json(
            self.history_dir / f"{task.view.id}.json",
            {"view": task.view.summary(), "identities": [asdict(i) for i in task.identities]},
        )

    def _update(self, task: _Task, *, persist: bool = True, **values: Any) -> None:
        task.view = replace(task.view, **values)
        if persist:
            try:
                self._persist(task)
            except OSError:
                # Summary failure is visible and stops later units. It cannot
                # erase a confirmed unit receipt or trigger automatic replay.
                task.view = replace(
                    task.view, error="Task history could not be saved", stop_requested=True
                )
        self._condition.notify_all()

    @staticmethod
    def _request_source(request: UnitRequest) -> str | None:
        from ultrakb.runtime.requests import ImportFile, ImportUrl

        if isinstance(request, ImportFile):
            return request.source
        if isinstance(request, ImportUrl):
            return request.url
        return None

    def submit(
        self,
        kb_dir: Path,
        requests: Sequence[UnitRequest],
        *,
        retry_of: str | None = None,
        require_verified_connection: bool = False,
        not_applicable: Sequence[TaskExclusion] = (),
    ) -> str:
        from ultrakb.lifecycle import current_generation

        root = kb_dir.expanduser().resolve()
        generation = current_generation(root)
        units = tuple(requests)
        if not units or len(units) > 10000 or not all(isinstance(r, REQUEST_TYPES) for r in units):
            raise ValueError("Submit 1–10000 supported execution requests")
        if not (root / ".ultrakb/config.yaml").is_file():
            raise ValueError("Open a knowledge base before submitting work")
        with self._condition:
            if not self._accepting:
                raise RuntimeError("Application is no longer accepting work")
            task_id = uuid.uuid4().hex
            view = TaskView(
                task_id,
                str(root),
                type(units[0]).__name__,
                "queued",
                "queued",
                len(units),
                (),
                False,
                True,
                retry_of=retry_of,
                not_applicable=tuple(not_applicable),
                current_source=self._request_source(units[0]),
            )
            task = _Task(
                view,
                units,
                tuple(
                    UnitIdentity.create(task_id, i, str(root), r, generation=generation)
                    for i, r in enumerate(units)
                ),
                require_verified_connection=(
                    require_verified_connection or self.require_verified_connection
                ),
                config_location=self._config_location(),
            )
            self._persist(task)  # A failed initial record means nothing was accepted.
            self._tasks[task_id] = task
            self._pending.append(task_id)
            self._condition.notify_all()
            return task_id

    def get(self, task_id: str) -> TaskView:
        with self._condition:
            return self._tasks[task_id].view

    @staticmethod
    def _config_location() -> tuple[Path, Path]:
        from ultrakb import config

        return config.GLOBAL_CONFIG_DIR, config.GLOBAL_CONFIG_PATH

    def tasks(self) -> tuple[TaskView, ...]:
        with self._condition:
            return tuple(task.view for task in self._tasks.values())

    def has_work(self, kb_dir: Path) -> bool:
        """This instance's queued execution or live handles, excluding old summaries."""
        root = str(kb_dir.expanduser().resolve())
        with self._condition:
            return any(attempt.identity.kb_dir == root for attempt in self._active.values()) or any(
                task.view.kb_dir == root
                and (task.view.state not in TERMINAL or task.view.pending_stops)
                for task in self._tasks.values()
            )

    def retry_inputs(
        self, task_id: str
    ) -> tuple[TaskView, tuple[UnitRequest, ...], tuple[UnitIdentity, ...]]:
        """Copy terminal task facts for read-only review outside the scheduler lock."""
        with self._condition:
            if task_id in self._clearing:
                raise ValueError("所选任务正在清理历史，请稍后再试")
            task = self._tasks[task_id]
            # Historical process cleanup may remain unverified forever. Only
            # handles owned by this manager block inspection/explicit cleanup;
            # this does not claim that a previous instance reaped its children.
            if (
                task_id in self._active
                or task.view.state not in TERMINAL
                or task.view.pending_stops
            ):
                raise ValueError("请等待任务结束并完成进程回收")
            return task.view, task.requests, task.identities

    def clear_history(self, task_ids: Sequence[str]) -> None:
        """Remove selected closed summaries/receipts; never touch KB artifacts."""
        ids = set(task_ids)
        with self._condition:
            for task_id in ids:
                self.retry_inputs(task_id)
            self._clearing.update(ids)
        removed = set()
        try:
            for task_id in ids:
                receipts = self.receipt_dir / task_id
                if receipts.is_symlink():
                    receipts.unlink()
                elif receipts.exists():
                    shutil.rmtree(receipts)
                # Keep the summary if deleting receipts fails. After a crash,
                # missing receipts prevent retry; summaries never replay work.
                (self.history_dir / f"{task_id}.json").unlink(missing_ok=True)
                removed.add(task_id)
        finally:
            with self._condition:
                for task_id in removed:
                    del self._tasks[task_id]
                self._clearing.difference_update(ids)
                self._pending = deque(
                    task_id for task_id in self._pending if task_id in self._tasks
                )
                self._condition.notify_all()

    def stop(
        self, task_id: str, *, schedule_control: Callable[[Callable[[], None]], None] | None = None
    ) -> None:
        pending_stops = []
        with self._condition:
            task = self._tasks[task_id]
            if task_id in self._stopping_controls:
                return
            if task.view.state in TERMINAL and not task.view.pending_stops:
                return
            from ultrakb.runtime.requests import RunPendingJob

            for index, request in enumerate(task.requests):
                if isinstance(request, RunPendingJob) and index >= len(task.view.results):
                    pending_stops.append(
                        PendingStop(
                            request.job_id,
                            request.dispatch_id,
                            task.identities[index].generation,
                            request.view_id,
                        )
                    )
            stops = task.view.pending_stops or tuple(pending_stops)
            if stops:
                self._stopping_controls.add(task_id)
            self._update(task, stop_requested=True, pending_stops=stops)
            if attempt := self._active.get(task_id):
                self._send_stop(task, attempt)
            if task_id not in self._active and task.view.state not in TERMINAL:
                self._update(task, state="stopped", stage="stopped")
                self._discard_inputs(task_id)
        from ultrakb.runtime.pending_control import confirm_stops

        def persist_controls():
            try:
                confirm_stops(Path(task.view.kb_dir), stops)
            except Exception as error:
                with self._condition:
                    self._stopping_controls.discard(task_id)
                    self._update(
                        task, stage="stop-unconfirmed", error=f"Pending stop not confirmed: {error}"
                    )
                raise
            with self._condition:
                self._stopping_controls.discard(task_id)
                self._update(
                    task,
                    pending_stops=(),
                    stage=task.view.state
                    if task.view.stage == "stop-unconfirmed" and task.view.state in TERMINAL
                    else task.view.stage,
                    error=None if task.view.stage == "stop-unconfirmed" else task.view.error,
                )

        if stops:
            if schedule_control is None:
                persist_controls()
            else:
                schedule_control(persist_controls)

    def release_input_wait(self, task_id: str) -> bool:
        """Return a never-started watch input to its bounded candidate scan.

        Only a reaped preparation attempt is eligible. An acknowledged config
        snapshot or any active child makes this operation refuse the release.
        """
        with self._condition:
            task = self._tasks[task_id]
            if (
                task_id in self._active
                or task.view.started_at is not None
                or task.view.state != "waiting"
                or task.view.stage != "waiting-input"
            ):
                return False
            self._update(task, state="stopped", stage="input-returned-to-watch")
            self._discard_inputs(task_id)
            return True

    def _discard_inputs(self, task_id: str, unit_id: str | None = None) -> None:
        root = Path(self._preparations.name) / task_id
        if unit_id is not None:
            root /= unit_id
        try:
            if root.exists():
                shutil.rmtree(root)
        except OSError:
            logging.getLogger(__name__).warning("Temporary task input cleanup failed")
            self._update(self._tasks[task_id], error="Temporary task input could not be removed")

    def wait(self, task_id: str, *, timeout: float = 60) -> TaskView:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                view = self._tasks[task_id].view
                if view.state in TERMINAL and task_id not in self._active:
                    return view
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Task remains {view.state}; no automatic termination")
                self._condition.wait(min(remaining, 0.1))

    def shutdown(
        self,
        *,
        stop: bool = False,
        schedule_control: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        with self._condition:
            self._accepting = False
            self._closing = True
            pending = (
                [
                    task.id
                    for task in self.tasks()
                    if task.state not in TERMINAL or task.pending_stops
                ]
                if stop
                else []
            )
            self._condition.notify_all()
        controls: list[Callable[[], None]] = []
        for task_id in pending:
            self.stop(task_id, schedule_control=controls.append)
        for control in controls:
            control() if schedule_control is None else schedule_control(control)

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _start(self, task: _Task) -> None:
        index = len(task.view.results)
        identity = task.identities[index]
        parent, child = self._context.Pipe()
        events = self._context.Queue(maxsize=128)
        prepared_dir = Path(self._preparations.name) / identity.task_id / identity.unit_id
        process = self._context.Process(
            target=run_unit,
            args=(
                task.requests[index],
                identity,
                task.snapshot,
                self.receipt_dir,
                child,
                events,
                prepared_dir,
                task.view.retry_of,
                task.require_verified_connection,
                task.config_location,
            ),
            name=f"ultrakb-unit-{identity.task_id[:8]}-{identity.unit_id}",
        )
        try:
            prepared_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            process.start()
        except Exception:
            parent.close()
            events.close()
            self._update(task, state="failed", stage="failed", error="Worker could not start")
            self._discard_inputs(task.view.id)
            return
        finally:
            child.close()
        self._active[task.view.id] = _Attempt(process, parent, events, identity)
        request = task.requests[index]
        self._update(
            task,
            state="running",
            stage="preparing",
            processes_reaped=False,
            current_source=self._request_source(request),
        )

    def _send_stop(self, task: _Task, attempt: _Attempt) -> None:
        if not attempt.stop_sent:
            try:
                attempt.control.send("stop")
            except (OSError, EOFError):
                pass
            attempt.stop_sent = True
            self._update(task, state="stopping", stage="stopping")

    def _control(self, task: _Task, attempt: _Attempt) -> None:
        if task.view.stop_requested:
            self._send_stop(task, attempt)
        if attempt.eof:
            return
        for _ in range(32):
            try:
                if not attempt.control.poll():
                    break
                message = attempt.control.recv()
            except (OSError, EOFError):
                attempt.eof = True
                break
            if message.get("identity") != asdict(attempt.identity):
                continue
            kind = message.get("kind")
            if kind == "snapshot":
                snapshot = message["snapshot"]
                if not isinstance(snapshot, ConfigSnapshot) or snapshot.kb_dir != task.view.kb_dir:
                    attempt.control.close()
                    attempt.eof = True
                    continue
                task.snapshot = snapshot
                self._update(
                    task, started_at=task.view.started_at or datetime.now(timezone.utc).isoformat()
                )
                try:
                    attempt.control.send("snapshot-ack")
                except (OSError, EOFError):
                    attempt.eof = True
            elif kind == "deferred":
                attempt.deferred = True
                attempt.deferred_reason = "input" if message.get("reason") == "input" else "lease"
            elif kind in ("result", "unconfirmed"):
                attempt.result = message["result"]
                attempt.terminal_sequence = message.get("sequence")
                attempt.unconfirmed = kind == "unconfirmed"
                if message.get("truncated"):
                    self._update(task, persist=False, text_truncated=True)

    def _progress(self, task: _Task, attempt: _Attempt) -> dict | None:
        latest_usage = None
        for _ in range(128):
            try:
                event = attempt.events.get_nowait()
            except (queue.Empty, OSError, EOFError, ValueError):
                break
            if event.get("identity") != asdict(attempt.identity):
                continue
            if event["sequence"] <= attempt.last_sequence:
                continue
            truncated = task.view.text_truncated or event["sequence"] != attempt.last_sequence + 1
            attempt.last_sequence = event["sequence"]
            data = event["data"]
            if data.get("event") == "compile_work":
                self._update(task, persist=False, compile_work=data["data"])
                continue
            if data.get("event") in {"model_usage", "model_usage_progress"}:
                # These are complete snapshots. Drain the backlog once, then
                # project its latest receipt outside the scheduler/UI lock.
                latest_usage = data["data"]
                if truncated != task.view.text_truncated:
                    self._update(task, persist=False, text_truncated=truncated)
                continue
            stage = data.get("stage", task.view.stage)
            text = task.view.text
            usage = task.view.usage
            model_usage = task.view.model_usage
            activities = {item["activity_id"]: item for item in task.view.activities}
            completed = set(task.view.completed_stages)
            source_count, unit_count = task.view.source_count, task.view.unit_count
            if data.get("event") == "activity":
                detail = data["data"]
                if detail["status"] == "started":
                    activities[detail["activity_id"]] = detail
                elif detail["activity_id"] in activities:
                    activities.pop(detail["activity_id"])
                    if detail["status"] == "completed":
                        completed.add(detail["stage"])
                    source_count = max(source_count, detail.get("source_count", 0))
                    unit_count = max(unit_count, detail.get("unit_count", 0))
            priority = {"answering": 1, "preparing": 2, "retrieving": 3, "reading": 4, "saving": 5}
            if activities:
                stage = max(activities.values(), key=lambda item: priority.get(item["stage"], 0))[
                    "stage"
                ]
            if data.get("event") == "answer_start":
                text = ""
            if data.get("event") == "usage":
                from ultrakb.agent.token_usage import TokenUsage

                usage = TokenUsage.from_dict(data["data"]).to_dict()
            if data.get("event") == "delta":
                text += data["data"]["text"]
                if len(text) > 1_000_000:
                    text, truncated = text[-1_000_000:], True
            self._update(
                task,
                persist=False,
                stage=stage,
                current_source=data.get("source", task.view.current_source),
                text=text,
                text_truncated=truncated,
                usage=usage,
                model_usage=model_usage,
                activities=tuple(activities.values()),
                completed_stages=tuple(sorted(completed)),
                source_count=source_count,
                unit_count=unit_count,
            )
        return latest_usage

    def _finish(self, task: _Task, attempt: _Attempt, *, model_usage=None) -> None:
        attempt.process.join()
        exitcode = attempt.process.exitcode
        attempt.control.close()
        attempt.events.close()
        attempt.events.join_thread()
        attempt.process.close()
        del self._active[task.view.id]
        self._update(task, processes_reaped=True, activities=())
        if attempt.deferred and exitcode == 0:
            if task.view.stop_requested:
                self._update(task, state="stopped", stage="stopped")
                self._discard_inputs(task.view.id)
            else:
                task.ready_at = time.monotonic() + task.wait_delay
                task.wait_delay = min(2, task.wait_delay * 2)
                self._update(
                    task,
                    state="waiting",
                    stage="waiting-input" if attempt.deferred_reason == "input" else "waiting",
                )
                self._pending.append(task.view.id)
            return
        self._discard_inputs(task.view.id, attempt.identity.unit_id)
        receipt = read_receipt(self.receipt_dir, attempt.identity)
        if receipt is None or attempt.unconfirmed:
            self._update(
                task,
                state="interrupted",
                stage="unconfirmed",
                error="Worker result could not be confirmed; remaining units were not started",
                text=attempt.result.output if attempt.result else task.view.text,
                model_usage=model_usage or task.view.model_usage,
            )
            return
        result = receipt
        if attempt.result is not None and attempt.result.summary() == receipt.summary():
            result = attempt.result
        current = task.requests[len(task.view.results)]
        if isinstance(current, RecompileDocument) and result.revision and not result.halt:
            # Only a durable receipt advances this batch's confirmed state.
            # The next worker still checks it under its own full-unit lease.
            index = len(task.view.results)
            task.requests = tuple(
                replace(request, version=result.revision)
                if i > index
                and isinstance(request, RecompileDocument)
                and request.version == current.version
                else request
                for i, request in enumerate(task.requests)
            )
        results = (*task.view.results, result)
        self._update(
            task,
            results=results,
            completed_stages=tuple(
                dict.fromkeys(
                    (
                        *task.view.completed_stages,
                        *(stage for unit in result.import_units for stage in unit.completed_stages),
                    )
                )
            ),
            error=result.error or task.view.error,
            text=result.output or task.view.text,
            model_usage=model_usage,
            text_truncated=task.view.text_truncated
            or (
                attempt.terminal_sequence is not None
                and attempt.terminal_sequence != attempt.last_sequence
            ),
        )
        if exitcode != 0:
            self._update(
                task,
                state="interrupted",
                stage="interrupted",
                error="Business result retained; worker exited abnormally",
            )
        elif task.view.stop_requested or result.status == "stopped":
            self._update(task, state="stopped", stage="stopped")
        elif result.halt:
            self._update(task, state="blocked", stage="blocked", error=result.error)
        elif result.output_state == "unavailable":
            self._update(
                task,
                state="partial",
                stage="output-unavailable",
                error="Execution confirmed; answer unavailable. Remaining units stopped.",
            )
        elif len(results) == task.view.total:
            failed = any(row.status == "failed" for row in results)
            blocked = any(row.status == "blocked" for row in results)
            state = (
                "partial"
                if (failed or blocked) and task.view.succeeded
                else "failed"
                if failed
                else "blocked"
                if blocked
                else "completed"
            )
            if any(row.status == "partial" for row in results):
                state = "partial"
            if result.status == "stopped":
                state = "stopped"
            self._update(task, state=state, stage=state)
        elif task.view.stop_requested or result.status == "stopped":
            self._update(task, state="stopped", stage="stopped")
        else:
            task.wait_delay = 0.25
            self._update(task, state="queued", stage="queued", text="")
            self._pending.append(task.view.id)

    def _run(self) -> None:
        from ultrakb.mutation import RecoveryRequired

        while True:
            usage_updates = []
            finished = []
            with self._condition:
                for task_id, attempt in list(self._active.items()):
                    task = self._tasks[task_id]
                    self._control(task, attempt)
                    latest_usage = self._progress(task, attempt)
                    if not attempt.process.is_alive():
                        self._control(task, attempt)
                        final_usage = self._progress(task, attempt)
                        if final_usage is not None:
                            latest_usage = final_usage
                        # Retain the final progress snapshot before confirming
                        # this child's result on the next scheduler iteration.
                        if latest_usage is None:
                            finished.append((task, attempt))
                    if latest_usage is not None:
                        usage_updates.append((task, attempt, latest_usage))
                for _ in range(len(self._pending)):
                    if len(self._active) >= self.max_workers:
                        break
                    from ultrakb.runtime.requests import RunPendingJob

                    # Explicit user work takes the next slot before queued derived jobs.
                    self._pending = deque(
                        sorted(
                            self._pending,
                            key=lambda identity: bool(
                                self._tasks[identity].requests
                                and isinstance(self._tasks[identity].requests[0], RunPendingJob)
                            ),
                        )
                    )
                    task_id = self._pending.popleft()
                    task = self._tasks[task_id]
                    if task.view.state in TERMINAL:
                        continue
                    busy = any(a.identity.kb_dir == task.view.kb_dir for a in self._active.values())
                    if busy or time.monotonic() < task.ready_at:
                        self._pending.append(task_id)
                        continue
                    self._start(task)
                if self._closing and not self._active and not self._pending:
                    try:
                        self._preparations.cleanup()
                    except OSError:
                        logging.getLogger(__name__).warning(
                            "Temporary task directory cleanup failed"
                        )
                    return
            for task, attempt, latest_usage in usage_updates:
                from ultrakb.lifecycle import expected_generation, read_lifecycle
                from ultrakb.llm_usage import merge_usage_receipts
                from ultrakb.usage_projection import merge_progress

                merge = (
                    merge_progress
                    if not latest_usage["current"].get("request_ids_complete", True)
                    else merge_usage_receipts
                )
                root = Path(task.view.kb_dir)
                try:
                    with (
                        expected_generation(root, attempt.identity.generation),
                        read_lifecycle(root),
                    ):
                        model_usage = merge(
                            root,
                            [*(result.model_usage for result in task.view.results), latest_usage],
                        )
                except (FileNotFoundError, ValueError, RecoveryRequired):
                    continue  # Late callbacks cannot write a replacement KB's projection.
                with self._condition:
                    if self._active.get(task.view.id) is attempt:
                        self._update(task, persist=False, model_usage=model_usage)
            for task, attempt in finished:
                from ultrakb.lifecycle import expected_generation, read_lifecycle
                from ultrakb.llm_usage import merge_usage_receipts
                from ultrakb.llm_usage_execution import task_usage_receipt

                # The terminal audit may scan the complete ledger. Status reads
                # and stop requests must remain available while it reconciles.
                root = Path(task.view.kb_dir)
                receipt = read_receipt(self.receipt_dir, attempt.identity)
                receipts = [
                    *(result.model_usage for result in task.view.results),
                    receipt.model_usage if receipt else None,
                ]
                try:
                    with (
                        expected_generation(root, attempt.identity.generation),
                        read_lifecycle(root),
                    ):
                        model_usage = task_usage_receipt(root, task.view.id)
                        if model_usage is None and any(receipts):
                            model_usage = merge_usage_receipts(root, receipts)
                except (FileNotFoundError, ValueError, RecoveryRequired):
                    model_usage = None
                model_usage = model_usage or task.view.model_usage
                with self._condition:
                    if self._active.get(task.view.id) is attempt:
                        self._finish(task, attempt, model_usage=model_usage)
            with self._condition:
                self._condition.wait(0.025)
