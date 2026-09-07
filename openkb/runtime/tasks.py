"""Bounded spawn scheduling independent of windows and progress observers."""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import queue
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from openkb.config_state import ConfigSnapshot
from openkb.locks import atomic_write_json
from openkb.runtime.records import TERMINAL, TaskView, UnitIdentity, UnitResult, read_receipt
from openkb.runtime.requests import REQUEST_TYPES, RecompileDocument, UnitRequest
from openkb.runtime.worker import run_unit


@dataclass
class _Task:
    view: TaskView
    requests: tuple[UnitRequest, ...] = field(default=(), repr=False)
    identities: tuple[UnitIdentity, ...] = ()
    snapshot: ConfigSnapshot | None = field(default=None, repr=False)
    ready_at: float = 0.0
    wait_delay: float = 0.25


@dataclass
class _Attempt:
    process: Any
    control: Any
    events: Any
    identity: UnitIdentity
    result: UnitResult | None = None
    deferred: bool = False
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

    def __init__(self, *, history_dir: Path, max_workers: int = 2) -> None:
        if not 1 <= max_workers <= 8:
            raise ValueError("Worker limit must be between 1 and 8")
        self.history_dir = history_dir.expanduser().resolve()
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.receipt_dir = self.history_dir / "units"
        self._preparations = tempfile.TemporaryDirectory(prefix="openkb-task-inputs-")
        self.max_workers = max_workers
        self._context = mp.get_context("spawn")
        self._condition = threading.Condition(threading.RLock())
        self._tasks: dict[str, _Task] = {}
        self._pending: deque[str] = deque()
        self._active: dict[str, _Attempt] = {}
        self._accepting = True
        self._closing = False
        self._load_history()
        self._thread = threading.Thread(target=self._run, name="openkb-tasks", daemon=False)
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

    def submit(self, kb_dir: Path, requests: Sequence[UnitRequest]) -> str:
        root = kb_dir.expanduser().resolve()
        units = tuple(requests)
        if not units or len(units) > 10000 or not all(isinstance(r, REQUEST_TYPES) for r in units):
            raise ValueError("Submit 1–10000 supported execution requests")
        if not (root / ".openkb/config.yaml").is_file():
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
            )
            task = _Task(
                view,
                units,
                tuple(UnitIdentity.create(task_id, i, str(root), r) for i, r in enumerate(units)),
            )
            self._persist(task)  # A failed initial record means nothing was accepted.
            self._tasks[task_id] = task
            self._pending.append(task_id)
            self._condition.notify_all()
            return task_id

    def get(self, task_id: str) -> TaskView:
        with self._condition:
            return self._tasks[task_id].view

    def tasks(self) -> tuple[TaskView, ...]:
        with self._condition:
            return tuple(task.view for task in self._tasks.values())

    def stop(self, task_id: str) -> None:
        with self._condition:
            task = self._tasks[task_id]
            self._update(task, stop_requested=True)
            if task_id not in self._active and task.view.state not in TERMINAL:
                self._update(task, state="stopped", stage="stopped")
                self._discard_inputs(task_id)

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

    def shutdown(self, *, stop: bool = False) -> None:
        with self._condition:
            self._accepting = False
            self._closing = True
            if stop:
                for task_id in self._tasks:
                    if self._tasks[task_id].view.state not in TERMINAL:
                        self.stop(task_id)
            self._condition.notify_all()

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
            ),
            name=f"openkb-unit-{identity.task_id[:8]}-{identity.unit_id}",
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
        self._update(task, state="running", stage="preparing", processes_reaped=False)

    def _control(self, task: _Task, attempt: _Attempt) -> None:
        if task.view.stop_requested and not attempt.stop_sent:
            try:
                attempt.control.send("stop")
            except (OSError, EOFError):
                pass
            attempt.stop_sent = True
            self._update(task, state="stopping", stage="stopping")
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
            elif kind in ("result", "unconfirmed"):
                attempt.result = message["result"]
                attempt.terminal_sequence = message.get("sequence")
                attempt.unconfirmed = kind == "unconfirmed"
                if message.get("truncated"):
                    self._update(task, persist=False, text_truncated=True)

    def _progress(self, task: _Task, attempt: _Attempt) -> None:
        for _ in range(128):
            try:
                event = attempt.events.get_nowait()
            except (queue.Empty, OSError, EOFError, ValueError):
                break
            if event.get("identity") != asdict(attempt.identity):
                continue
            truncated = task.view.text_truncated or event["sequence"] != attempt.last_sequence + 1
            attempt.last_sequence = event["sequence"]
            data = event["data"]
            stage = data.get("stage", task.view.stage)
            text = task.view.text
            if data.get("event") == "delta":
                text += data["data"]["text"]
                if len(text) > 1_000_000:
                    text, truncated = text[-1_000_000:], True
            self._update(task, persist=False, stage=stage, text=text, text_truncated=truncated)

    def _finish(self, task: _Task, attempt: _Attempt) -> None:
        attempt.process.join()
        exitcode = attempt.process.exitcode
        attempt.control.close()
        attempt.events.close()
        attempt.events.join_thread()
        attempt.process.close()
        del self._active[task.view.id]
        self._update(task, processes_reaped=True)
        if attempt.deferred and exitcode == 0:
            if task.view.stop_requested:
                self._update(task, state="stopped", stage="stopped")
                self._discard_inputs(task.view.id)
            else:
                task.ready_at = time.monotonic() + task.wait_delay
                task.wait_delay = min(2, task.wait_delay * 2)
                self._update(task, state="waiting", stage="waiting")
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
            error=result.error or task.view.error,
            text=result.output or task.view.text,
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
            state = (
                "partial" if failed and task.view.succeeded else "failed" if failed else "completed"
            )
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
        with self._condition:
            while True:
                for task_id, attempt in list(self._active.items()):
                    task = self._tasks[task_id]
                    self._control(task, attempt)
                    self._progress(task, attempt)
                    if not attempt.process.is_alive():
                        self._control(task, attempt)
                        self._progress(task, attempt)
                        self._finish(task, attempt)
                for _ in range(len(self._pending)):
                    if len(self._active) >= self.max_workers:
                        break
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
                self._condition.wait(0.025)
