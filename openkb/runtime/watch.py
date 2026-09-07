"""Bounded native raw subscriptions; each accepted file is an independent task.

Recursive incremental polling catches atomic renames without an unbounded OS
event queue. A temporary on-disk version index keeps rescans from replaying
failed business work; only the candidate window and submitted units live in
memory. Nothing restarts a subscription after application restart.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Generator

from openkb.inputs import SUPPORTED_EXTENSIONS
from openkb.lifecycle import (
    KnowledgeBaseRemoved,
    current_generation,
    expected_generation,
    read_lifecycle,
)
from openkb.locks import LockCancelled, kb_read_lock
from openkb.mutation import RecoveryRequired
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import ImportFile
from openkb.runtime.tasks import TaskManager
from openkb.state import HashRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchView:
    kb_dir: str
    state: str = "scanning"
    startup_found: int = 0
    submitted: int = 0
    pending: int = 0
    active: int = 0
    overflow_scans: int = 0
    scans: int = 0
    error: str | None = None


@dataclass
class _Candidate:
    stamp: str
    changed_at: float
    first_seen: float
    digest: str | None = None
    verified_at: float = 0


class _Busy(Exception):
    pass


def _busy() -> None:
    raise _Busy()


def _stamp(path: Path) -> str:
    value = path.stat()
    return f"{value.st_dev}:{value.st_ino}:{value.st_size}:{value.st_mtime_ns}"


def _files(directory: Path, report: Callable[[Path, OSError], None]) -> Generator[Path, None, None]:
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        yield from _files(Path(entry.path), report)
                    elif entry.is_file(follow_symlinks=False):
                        path = Path(entry.path)
                        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                            yield path
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    report(Path(entry.path), exc)
    except OSError as exc:
        report(directory, exc)


class NativeWatch:
    def __init__(
        self,
        kb_dir: Path,
        manager: TaskManager,
        *,
        debounce: float = 2,
        scan_interval: float = 1,
        capacity: int = 256,
        max_submitted: int = 32,
        verify_interval: float = 30,
    ) -> None:
        self.root = kb_dir.expanduser().resolve()
        self._generation = current_generation(self.root)
        if not (self.root / ".openkb/config.yaml").is_file():
            raise ValueError("Open a knowledge base before watching")
        if not 1 <= capacity <= 10000 or not 1 <= max_submitted <= 256:
            raise ValueError("Invalid watch capacity")
        if debounce <= 0 or scan_interval <= 0 or verify_interval <= 0:
            raise ValueError("Watch intervals must be positive")
        self.manager = manager
        self.debounce, self.scan_interval = debounce, scan_interval
        self.capacity, self.max_submitted = capacity, max_submitted
        self.verify_interval = verify_interval
        self._gate = threading.RLock()
        self._stop = threading.Event()
        self._view = WatchView(str(self.root))
        self._candidates: OrderedDict[Path, _Candidate] = OrderedDict()
        self._active: dict[Path, str] = {}
        self._halted: str | None = None
        self._scan_error: str | None = None
        self._thread = threading.Thread(target=self._run, name="openkb-native-watch", daemon=False)
        self._thread.start()

    def view(self) -> WatchView:
        with self._gate:
            view = self._view
            active = self._active.copy()
        count = 0
        for task in active.values():
            try:
                count += self.manager.get(task).state not in TERMINAL
            except KeyError:
                pass  # A stopped subscription can outlive cleared terminal history.
        return replace(view, active=count)

    def referenced_task_ids(self) -> frozenset[str]:
        with self._gate:
            return frozenset(self._active.values()) if self._thread.is_alive() else frozenset()

    def _update(self, **values) -> None:
        with self._gate:
            self._view = replace(self._view, **values)

    def stop(self) -> None:
        with self._gate:
            self._stop.set()
            self._view = replace(
                self._view, state="stopping" if self._thread.is_alive() else "stopped"
            )

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _scan_problem(self, path: Path, error: OSError) -> None:
        self._scan_error = f"补查暂无法读取（{type(error).__name__}）：{path.name}"

    def _remember(self, db, path, stamp, digest) -> None:
        db.execute(
            "INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?)",
            (str(path), stamp, digest, time.monotonic()),
        )

    def _reap(self, db) -> None:
        for path, task_id in list(self._active.items()):
            try:
                task = self.manager.get(task_id)
            except KeyError:
                self._halted = "监听正在确认的任务记录已被清理；请检查成果后重新启用监听。"
                del self._active[path]
                continue
            if task.state not in TERMINAL or not task.processes_reaped:
                continue
            if task.results and task.results[-1].revision:
                # A queued import can refresh to newer bytes before starting.
                # Even its failed processed version must not be replayed.
                self._remember(db, path, "", task.results[-1].revision)
            if task.state in {"interrupted", "blocked"}:
                self._halted = "监听任务成果需要核实或知识库需要修复；检查任务后可重新启用监听。"
            del self._active[path]

    def _discover(self, db, path: Path, now: float) -> None:
        if path in self._active:
            return
        stamp = _stamp(path)
        seen = db.execute("SELECT stamp, checked FROM seen WHERE path=?", (str(path),)).fetchone()
        if seen and seen[0] == stamp and now - seen[1] < self.verify_interval:
            return
        candidate = self._candidates.get(path)
        if candidate:
            if candidate.stamp != stamp:
                candidate.stamp, candidate.changed_at = stamp, now
                candidate.digest = None
            return
        if len(self._candidates) >= self.capacity:
            self._overflow = True
            oldest, waiting = next(iter(self._candidates.items()))
            if now - waiting.first_seen < max(1, self.debounce * 3):
                return
            # Rotate continuously changing/blocked inputs so they cannot fill
            # the entire candidate window forever. Subsequent scans revisit them.
            del self._candidates[oldest]
        self._candidates[path] = _Candidate(stamp, now, now)

    def _accept(self, db, path: Path, candidate: _Candidate, now: float) -> bool:
        if path.is_symlink() or not path.resolve().is_relative_to(self.root / "raw"):
            raise ValueError("Watched file moved outside raw")
        stamp = _stamp(path)
        if stamp != candidate.stamp:
            candidate.stamp, candidate.changed_at = stamp, now
            candidate.digest = None
            return False
        if now - candidate.changed_at < self.debounce:
            return False
        if candidate.digest is None or now - candidate.verified_at >= self.verify_interval:
            candidate.digest = HashRegistry.hash_file(path)
            candidate.verified_at = now
        digest = candidate.digest
        if _stamp(path) != stamp:
            candidate.changed_at = now
            candidate.digest = None
            return False
        seen = db.execute("SELECT digest FROM seen WHERE path=?", (str(path),)).fetchone()
        if seen and seen[0] == digest:
            self._remember(db, path, stamp, digest)
            return True
        with kb_read_lock(self.root / ".openkb", cancelled=self._stop.is_set, on_wait=_busy):
            registry = HashRegistry(self.root / ".openkb/hashes.json")
            if registry.is_known(digest):
                self._remember(db, path, stamp, digest)
                return True
        with self._gate:
            if self._stop.is_set():
                return False
            if len(self._active) >= self.max_submitted:
                for waiting_path, task_id in list(self._active.items()):
                    task = self.manager.get(task_id)
                    if task.state == "waiting" and task.stage == "waiting-input":
                        if self.manager.release_input_wait(task_id):
                            del self._active[waiting_path]
                            db.execute("DELETE FROM seen WHERE path=?", (str(waiting_path),))
                            break
                if len(self._active) >= self.max_submitted:
                    return False
            task_id = self.manager.submit(self.root, [ImportFile(str(path), wait_for_stable=True)])
            self._active[path] = task_id
            self._remember(db, path, stamp, digest)
            self._view = replace(self._view, submitted=self._view.submitted + 1)
        return True

    def _run(self) -> None:
        iterator = None
        try:
            raw = self.root / "raw"
            if raw.is_symlink() or not raw.is_dir():
                raise ValueError("Watch requires a local raw directory")
            with tempfile.TemporaryDirectory(prefix="openkb-watch-") as directory:
                db = sqlite3.connect(Path(directory) / "versions.sqlite")
                try:
                    db.execute(
                        "CREATE TABLE seen (path TEXT PRIMARY KEY, stamp TEXT, "
                        "digest TEXT, checked REAL)"
                    )
                    next_scan = 0.0
                    self._overflow = False
                    while not self._stop.is_set():
                        if current_generation(self.root) != self._generation:
                            raise KnowledgeBaseRemoved("Watched knowledge base was replaced")
                        if raw.is_symlink() or raw.resolve() != self.root / "raw":
                            raise ValueError("Watched raw directory changed location")
                        now = time.monotonic()
                        self._reap(db)
                        if iterator is None and now >= next_scan:
                            self._scan_error = None
                            iterator = _files(raw, self._scan_problem)
                            self._overflow = False
                        if iterator is not None:
                            for _ in range(128):
                                try:
                                    path = next(iterator)
                                except StopIteration:
                                    iterator = None
                                    self._update(
                                        scans=self._view.scans + 1,
                                        overflow_scans=self._view.overflow_scans
                                        + int(self._overflow),
                                    )
                                    next_scan = now + self.scan_interval
                                    break
                                if self._view.scans == 0:
                                    self._update(startup_found=self._view.startup_found + 1)
                                try:
                                    self._discover(db, path, now)
                                except FileNotFoundError:
                                    continue
                                except OSError as exc:
                                    self._scan_problem(path, exc)
                        state, error = (
                            ("blocked", self._halted)
                            if self._halted
                            else ("watching", self._scan_error)
                        )
                        for path, candidate in list(self._candidates.items()):
                            if self._stop.is_set() or self._halted:
                                break
                            try:
                                with (
                                    expected_generation(self.root, self._generation),
                                    read_lifecycle(
                                        self.root, cancelled=self._stop.is_set, on_wait=_busy
                                    ),
                                ):
                                    if self._accept(db, path, candidate, now):
                                        del self._candidates[path]
                            except KnowledgeBaseRemoved:
                                raise
                            except FileNotFoundError:
                                del self._candidates[path]
                            except _Busy:
                                state = "waiting"
                                break
                            except RecoveryRequired:
                                state, error = (
                                    "blocked",
                                    "知识库需要修复；监听尚未提交的文件等待修复。",
                                )
                                break
                            except OSError as exc:
                                state, error = (
                                    "waiting",
                                    f"输入暂不可读（{type(exc).__name__}）：{path.name}",
                                )
                                candidate.changed_at = now
                        db.commit()
                        self._update(
                            state=state,
                            error=error,
                            pending=len(self._candidates),
                            active=len(self._active),
                        )
                        self._stop.wait(min(0.1, self.scan_interval))
                finally:
                    if iterator is not None:
                        iterator.close()
                    db.close()
        except LockCancelled:
            pass
        except Exception as exc:
            logger.exception("Native watch stopped")
            self._update(
                state="failed", error=f"监听已结束（{type(exc).__name__}）。可检查后重新启用。"
            )
        finally:
            self._candidates.clear()
            if self._stop.is_set():
                self._update(state="stopped", pending=0)


class NativeWatchRegistry:
    def __init__(self, manager: TaskManager):
        self.manager = manager
        self._watches: dict[Path, NativeWatch] = {}
        self._accepting = True

    def start(self, root: Path) -> NativeWatch:
        root = root.expanduser().resolve()
        if not self._accepting:
            raise RuntimeError("Application is no longer accepting subscriptions")
        prior = self._watches.get(root)
        if prior and not prior.join(0):
            return prior
        watch = NativeWatch(root, self.manager)
        self._watches[root] = watch
        return watch

    def watches(self) -> tuple[NativeWatch, ...]:
        return tuple(self._watches.values())

    def stop_all(self, *, close: bool = True) -> None:
        self._accepting = not close
        for watch in self._watches.values():
            watch.stop()

    def stopped(self) -> bool:
        return all(watch.join(0) for watch in self._watches.values())
