"""Server-managed file watchers for the OpenKB REST API.

Each knowledge base may have at most one active watcher that observes its
``raw/`` directory and automatically ingests new/modified files through the
same locked pipeline used by the CLI ``add`` command and the REST ``/add``
endpoint (``openkb.cli._add_for_api``). The watcher runs entirely in
background threads; the FastAPI layer only starts/stops/reads state.

Design notes:
* One worker thread per KB consumes debounced file batches sequentially, so
  ingest is serialized per-KB (the global ingest lock would serialize it
  anyway) while different KBs proceed in parallel.
* Per-KB mutable state is held in :class:`WatcherState`; the registry guards
  only the KB->state map. Events live in a bounded ring buffer so a long-lived
  watcher never accumulates unbounded memory.
* ``_add_for_api`` and ``SUPPORTED_EXTENSIONS`` are imported into this module's
  namespace so tests can monkeypatch ``openkb.watch_service._add_for_api``.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchdog.observers import Observer

from openkb.application.documents import _add_for_api
from openkb.config import LlmCredentialBundle, resolve_credential_bundle
from openkb.inputs import SUPPORTED_EXTENSIONS
from openkb.lifecycle import (
    KnowledgeBaseIncomplete,
    KnowledgeBaseRemoved,
    current_generation,
    expected_generation,
)
from openkb.locks import LockCancelled, kb_ingest_lock
from openkb.mutation import RecoveryRequired
from openkb.watcher import start_watch

# How many recent events to retain per KB for status() and SSE replay.
_MAX_EVENTS = int(os.environ.get("OPENKB_WATCH_MAX_EVENTS", "200"))


@dataclass
class WatcherState:
    """Runtime state for a single KB's watcher."""

    kb: str
    kb_dir: Path
    raw_dir: Path
    debounce: float
    started_at: float
    observer: Observer | None = None
    worker_thread: threading.Thread | None = None
    queue: queue.Queue = field(default_factory=queue.Queue)
    running: threading.Event = field(default_factory=threading.Event)
    events: deque = field(default_factory=lambda: deque(maxlen=_MAX_EVENTS))
    counters: dict[str, int] = field(
        default_factory=lambda: {"added": 0, "skipped": 0, "failed": 0}
    )
    bundle: LlmCredentialBundle | None = None
    generation: str | None = None
    receiving: bool = True
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _gate: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.kb_dir is not None:
            self.generation = current_generation(self.kb_dir)
        self.running.set()


def _record_event(state: WatcherState, event: str, data: dict[str, Any]) -> None:
    """Append a timestamped, sequenced event to the per-KB ring buffer."""
    with state._lock:
        state._seq += 1
        seq = state._seq
        state.events.append({"seq": seq, "ts": time.time(), "event": event, "data": data})


def _inc(state: WatcherState, key: str) -> None:
    with state._lock:
        state.counters[key] = state.counters.get(key, 0) + 1


def _public_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Strip the internal ``seq`` before exposing an event over the API."""
    return {"ts": ev["ts"], "event": ev["event"], "data": ev.get("data", {})}


def _run_worker(state: WatcherState) -> None:
    """Consume debounced file batches and ingest each file.

    Exits when ``stop`` puts the ``None`` sentinel. Any per-file exception is
    recorded as an ``error`` event; the worker never propagates so one bad
    file can't kill the watcher.
    """
    try:
        while True:
            try:
                paths = state.queue.get()
            except Exception:
                return
            if paths is None:
                return
            for raw_path in paths:
                with expected_generation(state.kb_dir, state.generation):
                    _process_file(state, raw_path)
    except (RecoveryRequired, KnowledgeBaseRemoved, KnowledgeBaseIncomplete):
        _record_event(
            state,
            "error",
            {
                "path": str(state.kb_dir),
                "original_name": "",
                "message": (
                    "Watching stopped: knowledge base unavailable or needs repair; "
                    "remaining files were not processed."
                ),
            },
        )
        _record_event(state, "watcher_stopped", {"kb": state.kb})
    finally:
        with state._gate:
            state.receiving = False
        _stop_observer(state)
        # Ensure `running` reflects reality even on unexpected exit, so
        # status() reports inactive and start() can spawn a fresh worker.
        state.running.clear()


def _process_file(state: WatcherState, raw_path: str) -> None:
    path = Path(raw_path)
    suffix = path.suffix.lower()
    if not suffix or suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        _record_event(
            state,
            "file_done",
            {
                "path": str(path),
                "original_name": path.name,
                "status": "skipped",
                "message": f"Skipping unsupported file type: {suffix}. Supported: {supported}",
            },
        )
        _inc(state, "skipped")
        return
    _record_event(
        state,
        "file_start",
        {
            "path": str(path),
            "original_name": path.name,
        },
    )
    try:
        with kb_ingest_lock(state.kb_dir / ".openkb"):
            if state.raw_dir.resolve() != state.kb_dir / "raw":
                raise ValueError("Watched input moved outside the knowledge-base raw directory")
            result = _add_for_api(
                path, state.kb_dir, bundle=state.bundle, source_root=state.kb_dir / "raw"
            )
    except Exception as exc:  # worker must never die
        _record_event(
            state,
            "error",
            {
                "path": str(path),
                "original_name": path.name,
                "message": f"Failed to add: {path.name}: {exc}",
            },
        )
        _inc(state, "failed")
        if isinstance(exc, (RecoveryRequired, KnowledgeBaseRemoved, KnowledgeBaseIncomplete)):
            raise
        return
    status = result.status if result.status in ("added", "skipped", "failed") else "failed"
    _record_event(
        state,
        "file_done",
        {
            "path": str(path),
            "original_name": path.name,
            "status": status,
            "message": result.message,
        },
    )
    _inc(state, status)


def _stop_observer(state: WatcherState) -> None:
    try:
        if state.observer is not None:
            state.observer.stop()
            state.observer.join(timeout=5.0)
    except Exception:
        pass  # Closing the receiving gate still prevents late callbacks from queuing work.


@dataclass
class _Starting:
    cancelled: threading.Event = field(default_factory=threading.Event)
    result: Future[WatcherState] = field(default_factory=Future)


class WatchRegistry:
    """Thread-safe registry of per-KB watchers (one app instance owns one)."""

    def __init__(self, max_events: int = _MAX_EVENTS) -> None:
        self._watchers: dict[str, WatcherState] = {}
        self._starting: dict[str, _Starting] = {}
        self._lock = threading.Lock()
        self._max_events = max_events

    def _live(self, kb: str) -> WatcherState | None:
        state = self._watchers.get(kb)
        if (
            state
            and state.running.is_set()
            and state.worker_thread
            and state.worker_thread.is_alive()
        ):
            return state
        return None

    def start(self, kb: str, kb_dir: Path, debounce: float = 2.0) -> WatcherState:
        """Start one subscription, retaining the REST credential timing and drain policy."""
        with self._lock:
            if existing := self._live(kb):
                return existing
            pending = self._starting.get(kb)
            owner = pending is None
            if pending is None:
                pending = _Starting()
                self._starting[kb] = pending
        if not owner:
            return pending.result.result()
        try:
            state = self._start(kb, kb_dir, debounce, pending.cancelled)
        except BaseException as exc:
            pending.result.set_exception(exc)
            raise
        else:
            pending.result.set_result(state)
            return state
        finally:
            with self._lock:
                if self._starting.get(kb) is pending:
                    del self._starting[kb]

    def _start(
        self, kb: str, kb_dir: Path, debounce: float, cancelled: threading.Event
    ) -> WatcherState:
        root = kb_dir.expanduser().resolve()
        generation = current_generation(root)
        if not (root / ".openkb/config.yaml").is_file():
            raise ValueError("Open a knowledge base before watching")
        bundle = resolve_credential_bundle(root)
        # No registry mutex is held while waiting for another KB operation.
        with (
            expected_generation(root, generation),
            kb_ingest_lock(root / ".openkb", cancelled=cancelled.is_set),
        ):
            if not (root / ".openkb/config.yaml").is_file():
                raise ValueError("Knowledge base is no longer initialized")
            raw_dir = root / "raw"
            if raw_dir.is_symlink():
                raise ValueError("Watch requires a local raw directory")
            raw_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                if cancelled.is_set():
                    raise LockCancelled("Watcher start cancelled")
                state = WatcherState(
                    kb=kb,
                    kb_dir=root,
                    raw_dir=raw_dir,
                    debounce=debounce,
                    bundle=bundle,
                    started_at=time.time(),
                    events=deque(maxlen=self._max_events),
                )

                def on_new_files(paths: list[str]) -> None:
                    with state._gate:
                        if state.receiving:
                            state.queue.put(paths)

                state.observer = start_watch(raw_dir, on_new_files, debounce=debounce)
                state.worker_thread = threading.Thread(
                    target=_run_worker,
                    args=(state,),
                    daemon=True,
                    name=f"openkb-watch-{kb}",
                )
                try:
                    state.worker_thread.start()
                except BaseException:
                    with state._gate:
                        state.receiving = False
                    _stop_observer(state)
                    raise
                self._watchers[kb] = state
                return state

    def get(self, kb: str) -> WatcherState | None:
        with self._lock:
            return self._watchers.get(kb)

    def recent_events(self, kb: str) -> list[dict[str, Any]]:
        """Return public (seq-stripped) recent events for *kb* (empty if none)."""
        with self._lock:
            state = self._watchers.get(kb)
        if state is None:
            return []
        return [_public_event(e) for e in state.events]

    def status(self, kb: str) -> dict[str, Any]:
        with self._lock:
            state = self._watchers.get(kb)
            if state is None:
                return {"kb": kb, "active": False}
            with state._lock:
                counters = dict(state.counters)
            return {
                "kb": state.kb,
                "active": state.running.is_set(),
                "started_at": state.started_at,
                "raw_dir": str(state.raw_dir),
                "debounce": state.debounce,
                "counters": counters,
                "recent_events": [_public_event(e) for e in state.events],
            }

    def list_active(self) -> list[str]:
        with self._lock:
            return list(self._watchers.keys())

    def stop(self, kb: str) -> bool:
        """Stop and remove *kb*'s watcher. Return False if none was active.

        Joins the worker *before* clearing ``running`` and popping the state,
        so ``status()`` reports ``active: True`` (draining) while the old
        worker finishes. If the worker does not exit within the join timeout,
        the state is left in place with a ``watcher_draining`` event so
        ``start()`` knows not to spawn a second worker.
        """
        with self._lock:
            pending = self._starting.get(kb)
            if pending:
                pending.cancelled.set()
            state = self._watchers.get(kb)
        if state is None:
            return pending is not None
        # Close reception before withdrawing the OS subscription. Already queued
        # REST work retains the legacy drain-on-stop behavior.
        with state._gate:
            state.receiving = False
        _stop_observer(state)
        state.queue.put(None)
        if state.worker_thread is not None:
            state.worker_thread.join(timeout=5.0)

        # If the worker is still alive after the join timeout, it is likely
        # mid-compile. Do not pop the state or clear running — leave a
        # draining marker so start() refuses to spawn a duplicate.
        if state.worker_thread is not None and state.worker_thread.is_alive():
            _record_event(state, "watcher_draining", {"kb": kb})
            return True

        # Worker has exited: safe to finalize.
        state.running.clear()
        _record_event(state, "watcher_stopped", {"kb": kb})
        with self._lock:
            # Only pop if it is still the same state (not replaced by start()).
            if self._watchers.get(kb) is state:
                self._watchers.pop(kb, None)
        return True

    def stop_all(self) -> None:
        with self._lock:
            names = set(self._watchers) | set(self._starting)
        for kb in names:
            self.stop(kb)
