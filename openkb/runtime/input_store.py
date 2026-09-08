"""Private task inputs with parent/worker leases and restart-time orphan cleanup."""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import portalocker

from openkb.locks import atomic_write_json, file_write_lock


def _lease(path: Path) -> portalocker.Lock:
    # These are resource lifetimes, not thread/task-reentrant KB authority.
    # A manager may create its handle on one thread and release it on another.
    return portalocker.Lock(
        path, mode="a", timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB
    )


def _local(path: Path) -> bool:
    return not path.is_symlink() and path.resolve() == path


def _reap_locked(group: Path) -> bool:
    """Caller holds the collection gate; new workers cannot attach during removal."""
    if not group.is_dir() or not _local(group):
        return False
    if not _local(group / "owner.json"):
        return False
    try:
        if json.loads((group / "owner.json").read_text("utf-8")) != {
            "version": 1,
            "owner": group.name,
        }:
            return False
    except (OSError, ValueError):
        return False
    leases = group / "leases"
    data = group / "data"
    if not _local(leases) or not _local(data):
        return False
    for path in leases.iterdir():
        if not path.is_file() or not _local(path):
            return False
        try:
            # Every attachment requires the collection gate. A released lease
            # cannot be taken again during this scan, so one handle suffices.
            with _lease(path):
                pass
        except portalocker.exceptions.LockException:
            return False  # A live parent or worker still owns this input.
    if data.exists():
        shutil.rmtree(data)
    # Windows requires all lease handles closed before removal.
    shutil.rmtree(group)
    return True


def reap_orphaned_inputs(group: Path) -> None:
    base = group.parent
    if not _local(base) or not re.fullmatch(r"[0-9a-f]{32}", group.name):
        return
    with file_write_lock(base / ".collection.lock"):
        _reap_locked(group)


class InputStore:
    """One application's private roots, retained only while a parent/worker lives."""

    def __init__(self, history_dir: Path) -> None:
        base = history_dir / ".inputs"
        if not _local(base):
            raise ValueError("Task input storage must remain in its history directory")
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_write_lock(base / ".collection.lock"):
            for previous in base.iterdir():
                if re.fullmatch(r"[0-9a-f]{32}", previous.name):
                    try:
                        _reap_locked(previous)
                    except OSError:
                        logging.getLogger(__name__).warning("Previous task input cleanup failed")
            self.group = base / uuid.uuid4().hex
            self.group.mkdir(mode=0o700)
            (self.group / "leases").mkdir(mode=0o700)
            self._owner = _lease(self.group / "leases/manager")
            self._owner.acquire()
            try:
                atomic_write_json(
                    self.group / "owner.json", {"version": 1, "owner": self.group.name}
                )
                (self.group / "data").mkdir(mode=0o700)
            except BaseException:
                self._owner.release()
                raise
        self.name = str(self.group / "data")

    def cleanup(self) -> None:
        self._owner.release()
        reap_orphaned_inputs(self.group)


@contextmanager
def child_preparation(directory: Path | None) -> Iterator[None]:
    """Attach before using a unit's files; a dead parent's data cannot be revived."""
    if directory is None:
        yield
        return
    group = directory.parents[2]
    base = group.parent
    if directory.parents[1].name != "data" or not re.fullmatch(r"[0-9a-f]{32}", group.name):
        raise ValueError("Invalid task input location")
    owner = None
    try:
        with file_write_lock(base / ".collection.lock"):
            if not _local(directory) or not directory.is_dir():
                raise FileNotFoundError("Task input owner has exited")
            owner = _lease(group / "leases" / f"{directory.parent.name}-{directory.name}")
            owner.acquire()
        yield
    finally:
        if owner is not None:
            owner.release()
