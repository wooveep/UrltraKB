"""Cooperative filesystem locks and atomic writes for UltraKB.

The lock protocol is advisory and intended for local filesystem access by
UltraKB processes. It does not guarantee cross-host coordination on networked
or synced filesystems where the underlying OS lock may be unavailable or
inconsistent.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import IO, BinaryIO, Iterator

import portalocker


def flock(fh: IO, *, exclusive: bool) -> None:
    """Acquire an advisory lock on an open file handle (cross-platform).

    Delegates to :mod:`portalocker`:

    - **POSIX** — ``fcntl.flock``; the call blocks indefinitely until acquired.
    - **Windows** — shared locks use the Win32 ``LockFileEx`` API (``pywin32``,
      which portalocker pulls in automatically on Windows), so concurrent
      readers are honoured; exclusive locks use ``msvcrt.locking``, which
      retries for ~10s and then raises rather than blocking indefinitely.

    On failure portalocker raises :class:`portalocker.LockException` — note this
    is *not* an ``OSError`` (e.g. on filesystems without working lock support).
    """
    portalocker.lock(fh, portalocker.LOCK_EX if exclusive else portalocker.LOCK_SH)


def funlock(fh: IO) -> None:
    """Release a lock previously acquired with :func:`flock`."""
    portalocker.unlock(fh)


_LOCKS_GUARD = threading.Lock()
# An asyncio task is part of ownership: copied contexts and sibling tasks on
# the same thread must never inherit another operation's reentrant privilege.
_HELD_LOCKS: dict[tuple[Path, int, object], tuple[int, bool, IO]] = {}


class LockCancelled(RuntimeError):
    """Execution was stopped before it obtained permission to write."""


def _owner() -> tuple[int, object]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


class _Lease:
    def __init__(self, path: Path, exclusive: bool) -> None:
        self.path = path.resolve()
        self.exclusive = exclusive
        self.key = (self.path, *_owner())
        self.acquired = False
        self.first = False

    def try_acquire(self) -> bool:
        with _LOCKS_GUARD:
            held = _HELD_LOCKS.get(self.key)
            if held:
                depth, exclusive, fh = held
                if self.exclusive and not exclusive:
                    raise RuntimeError("Cannot upgrade an existing KB read lock to a write lock")
                _HELD_LOCKS[self.key] = (depth + 1, exclusive, fh)
                self.acquired = True
                return True
            for key, (_, exclusive, _) in _HELD_LOCKS.items():
                if key[0] == self.path and (self.exclusive or exclusive):
                    return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = self.path.open("a+", encoding="utf-8")
            flags = portalocker.LOCK_EX if self.exclusive else portalocker.LOCK_SH
            try:
                portalocker.lock(fh, flags | portalocker.LOCK_NB)
            except portalocker.exceptions.AlreadyLocked:
                fh.close()
                return False
            except BaseException:
                fh.close()
                raise
            _HELD_LOCKS[self.key] = (1, self.exclusive, fh)
            self.acquired = self.first = True
            return True

    def release(self) -> None:
        if not self.acquired:
            return
        with _LOCKS_GUARD:
            depth, exclusive, fh = _HELD_LOCKS[self.key]
            if depth > 1:
                _HELD_LOCKS[self.key] = (depth - 1, exclusive, fh)
            else:
                try:
                    funlock(fh)
                finally:
                    fh.close()
                    del _HELD_LOCKS[self.key]
            self.acquired = False


def _check_wait(cancelled: Callable[[], bool] | None, deadline: float | None) -> None:
    if cancelled is not None and cancelled():
        raise LockCancelled("Stopped while waiting for knowledge-base execution")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Timed out waiting for knowledge-base execution")


@contextlib.contextmanager
def file_write_lock(
    path: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[bool]:
    """Protect shared non-KB state, with the same owner and OS lock protocol.

    Yield whether this is the outer acquisition so recovery runs once, before
    a mutation starts. The lock pathname remains compatible with older writers.
    """
    lease = _Lease(path, True)
    try:
        while True:
            _check_wait(cancelled, None)
            if lease.try_acquire():
                break
            if on_wait:
                on_wait()
            time.sleep(0.05)
        _check_wait(cancelled, None)
        yield lease.first
    finally:
        lease.release()


def file_lock_held(path: Path, *, exclusive: bool = False) -> bool:
    """Whether this thread and asyncio task own the requested file lease."""
    key = (path.resolve(), *_owner())
    with _LOCKS_GUARD:
        held = _HELD_LOCKS.get(key)
        return bool(held and (not exclusive or held[1]))


def file_write_lock_held(path: Path) -> bool:
    return file_lock_held(path, exclusive=True)


class DelegatedWriteLease:
    """Explicit, revocable permission for SDK tool mutations within one lease.

    This never makes lock acquisition reentrant in another task. Only mutation
    code receiving this object can use it; it expires before its owner releases
    the OS lock and cannot authorize a later acquisition of the same pathname.
    """

    def __init__(self, path: Path) -> None:
        self.key = (path.resolve(), *_owner())
        with _LOCKS_GUARD:
            held = _HELD_LOCKS.get(self.key)
            if not held or not held[1]:
                raise RuntimeError("Only the write lease owner can delegate tool writes")
            self.handle = held[2]
        self.active = True

    def authorizes(self, path: Path) -> bool:
        with _LOCKS_GUARD:
            held = _HELD_LOCKS.get(self.key)
            return bool(
                self.active
                and self.key[0] == path.resolve()
                and held
                and held[1]
                and held[2] is self.handle
            )

    def revoke(self) -> None:
        self.active = False


def _drain_pending_journals(ultrakb_dir: Path) -> None:
    from ultrakb.mutation import recover_pending_journals

    for message in recover_pending_journals(ultrakb_dir.parent):
        logging.getLogger(__name__).warning(message)


def _pending_recovery(ultrakb_dir: Path) -> bool:
    return (ultrakb_dir / "needs-repair.json").exists() or any(
        (ultrakb_dir / "journal").glob("*.json")
    )


@contextlib.contextmanager
def kb_lock(
    ultrakb_dir: Path,
    *,
    exclusive: bool,
    cancelled: Callable[[], bool] | None = None,
    on_wait: Callable[[], None] | None = None,
    deadline: float | None = None,
) -> Iterator[None]:
    """Hold a canonical KB lock with cancellable, unbounded contention waiting.

    Async entry points use async_kb_lock so contention never blocks their loop.
    A deadline, if supplied, is an absolute time.monotonic() value.
    """
    from ultrakb.lifecycle import read_lifecycle

    with read_lifecycle(
        ultrakb_dir.parent, cancelled=cancelled, on_wait=on_wait, deadline=deadline
    ):
        lease = _Lease(ultrakb_dir / "ingest.lock", exclusive)
        notified = False
        try:
            while True:
                _check_wait(cancelled, deadline)
                if lease.try_acquire():
                    break
                if on_wait and not notified:
                    on_wait()
                    notified = True
                time.sleep(0.05)
            if lease.first:
                if exclusive:
                    _drain_pending_journals(ultrakb_dir)
                elif _pending_recovery(ultrakb_dir):
                    # Release the read lease before independent exclusive recovery;
                    # never upgrade a held shared lock in place.
                    lease.release()
                    with kb_lock(
                        ultrakb_dir,
                        exclusive=True,
                        cancelled=cancelled,
                        on_wait=on_wait,
                        deadline=deadline,
                    ):
                        pass
                    with kb_lock(
                        ultrakb_dir,
                        exclusive=False,
                        cancelled=cancelled,
                        on_wait=on_wait,
                        deadline=deadline,
                    ):
                        yield
                    return
            from ultrakb.lifecycle import validate_execution

            validate_execution(ultrakb_dir.parent)
            _check_wait(cancelled, deadline)
            yield
        finally:
            lease.release()


@contextlib.asynccontextmanager
async def async_kb_lock(
    ultrakb_dir: Path,
    *,
    exclusive: bool,
    cancelled: Callable[[], bool] | None = None,
    on_wait: Callable[[], None] | None = None,
    deadline: float | None = None,
) -> AsyncIterator[None]:
    """Keep acquisition, protected execution and release in the owning task."""
    from ultrakb.lifecycle import async_read_lifecycle

    async with async_read_lifecycle(
        ultrakb_dir.parent, cancelled=cancelled, on_wait=on_wait, deadline=deadline
    ):
        lease = _Lease(ultrakb_dir / "ingest.lock", exclusive)
        notified = False
        try:
            while True:
                _check_wait(cancelled, deadline)
                if lease.try_acquire():
                    break
                if on_wait and not notified:
                    on_wait()
                    notified = True
                await asyncio.sleep(0.05)
            if lease.first:
                if exclusive:
                    _drain_pending_journals(ultrakb_dir)
                elif _pending_recovery(ultrakb_dir):
                    lease.release()
                    async with async_kb_lock(
                        ultrakb_dir,
                        exclusive=True,
                        cancelled=cancelled,
                        on_wait=on_wait,
                        deadline=deadline,
                    ):
                        pass
                    async with async_kb_lock(
                        ultrakb_dir,
                        exclusive=False,
                        cancelled=cancelled,
                        on_wait=on_wait,
                        deadline=deadline,
                    ):
                        yield
                    return
            from ultrakb.lifecycle import validate_execution

            validate_execution(ultrakb_dir.parent)
            _check_wait(cancelled, deadline)
            yield
        finally:
            lease.release()


def kb_ingest_lock(ultrakb_dir: Path, **kwargs):
    """Hold an exclusive KB mutation lock."""
    return kb_lock(ultrakb_dir, exclusive=True, **kwargs)


def kb_read_lock(ultrakb_dir: Path, **kwargs):
    """Read consistent committed content, recovering interrupted mutations first."""
    return kb_lock(ultrakb_dir, exclusive=False, **kwargs)


@contextlib.contextmanager
def atomic_record_lock(path: Path) -> Iterator[None]:
    """Serialize independent atomic records without joining KB rollback journals."""
    lease = _Lease(path, True)
    try:
        while not lease.try_acquire():
            time.sleep(0.01)
        yield
    finally:
        lease.release()


def kb_ingest_lock_held(ultrakb_dir: Path) -> bool:
    """Whether this thread AND asyncio task owns exclusive execution."""
    return file_write_lock_held(ultrakb_dir / "ingest.lock")


@contextlib.contextmanager
def kb_repair_lock(ultrakb_dir: Path, **wait_options) -> Iterator[None]:
    """Exclusive access for controlled diagnostics/repair, without auto-recovery.

    Normal business operations always use kb_lock. Repair must inspect failed
    evidence while the durable marker is still present, then explicitly verify
    recovery before it clears that marker.
    """
    from ultrakb.lifecycle import read_lifecycle

    with read_lifecycle(ultrakb_dir.parent, **wait_options):
        if not ultrakb_dir.is_dir():
            raise FileNotFoundError(f"Knowledge base not found: {ultrakb_dir.parent}")
        with file_write_lock(ultrakb_dir / "ingest.lock", **wait_options):
            yield


def _session_lease(kb_dir: Path, session_id: str) -> _Lease:
    name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    lease = _Lease(kb_dir / ".ultrakb/session-locks" / f"{name}.lock", True)
    with _LOCKS_GUARD:
        nested = lease.key in _HELD_LOCKS
    if not nested and kb_ingest_lock_held(kb_dir / ".ultrakb"):
        raise RuntimeError("Acquire the conversation lock before the knowledge-base lock")
    return lease


@contextlib.contextmanager
def session_lock(
    kb_dir: Path,
    session_id: str,
    *,
    cancelled: Callable[[], bool] | None = None,
    on_wait: Callable[[], None] | None = None,
    database_identity=None,
) -> Iterator[None]:
    """Conversation identity survives deletion; acquire this before the KB lock."""
    from ultrakb.lifecycle import read_lifecycle

    with read_lifecycle(
        kb_dir, cancelled=cancelled, on_wait=on_wait, database_identity=database_identity
    ):
        lease = _session_lease(kb_dir, session_id)
        notified = False
        try:
            while True:
                _check_wait(cancelled, None)
                if lease.try_acquire():
                    break
                if on_wait and not notified:
                    on_wait()
                    notified = True
                time.sleep(0.05)
            _check_wait(cancelled, None)
            yield
        finally:
            lease.release()


@contextlib.asynccontextmanager
async def async_session_lock(
    kb_dir: Path,
    session_id: str,
    *,
    cancelled: Callable[[], bool] | None = None,
    on_wait: Callable[[], None] | None = None,
    database_identity=None,
) -> AsyncIterator[None]:
    from ultrakb.lifecycle import async_read_lifecycle

    async with async_read_lifecycle(
        kb_dir, cancelled=cancelled, on_wait=on_wait, database_identity=database_identity
    ):
        lease = _session_lease(kb_dir, session_id)
        notified = False
        try:
            while True:
                _check_wait(cancelled, None)
                if lease.try_acquire():
                    break
                if on_wait and not notified:
                    on_wait()
                    notified = True
                await asyncio.sleep(0.05)
            _check_wait(cancelled, None)
            yield
        finally:
            lease.release()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows cannot open a directory handle to fsync it. os.replace is
        # atomic on NTFS (no torn/partial state), though without the dir flush
        # the rename's durability across a crash is weaker than on POSIX.
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _default_file_mode() -> int:
    current_umask = os.umask(0)
    os.umask(current_umask)
    return 0o666 & ~current_umask


def _target_mode(path: Path) -> int:
    try:
        return path.stat().st_mode & 0o777
    except FileNotFoundError:
        return _default_file_mode()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically replace *path* with binary *content*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            if hasattr(os, "fchmod"):  # not available on Windows
                os.fchmod(fh.fileno(), _target_mode(path))
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


class PrivateMaterialization:
    """Fresh disposable input only; durable files continue using atomic writes.

    Targets are always inside a new TemporaryDirectory created here. A caller
    cannot select an existing wiki or CAS directory or reuse a dirty baseline.
    """

    def __init__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="ultrakb-compile-")
        self._owner = _owner()
        self._active = True
        self.bytes_copied = 0

    @property
    def directory(self) -> Path:
        return Path(self._temporary.name)

    def permits_scope(self, directory: Path) -> bool:
        return self._active and self._owner[0] == _owner()[0] and directory == self.directory

    def copy(
        self,
        source: Path | BinaryIO,
        relative: str,
        *,
        digest: str,
        byte_count: int,
        replace: bool = False,
    ) -> None:
        if not self._active or self._owner != _owner():
            raise RuntimeError("Private materialization is closed or owned by another execution")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or ":" in relative
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError("Private materialization requires a logical relative path")
        target = self.directory.joinpath(*relative.split("/"))
        if target.is_symlink():
            raise ValueError("Private materialization cannot follow target symlinks")
        for parent in (self.directory, *target.parents):
            if parent == self.directory.parent:
                break
            if parent.is_symlink():
                raise ValueError("Private materialization cannot follow target symlinks")
        target.parent.mkdir(parents=True, exist_ok=True)
        hasher, size, created = hashlib.sha256(), 0, False
        try:
            reader_context = (
                source.open("rb") if isinstance(source, Path) else contextlib.nullcontext(source)
            )
            with reader_context as reader, target.open("wb" if replace else "xb") as writer:
                created = True
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
            if size != byte_count or hasher.hexdigest() != digest:
                raise ValueError("Private materialization failed content integrity validation")
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
        self.bytes_copied += size

    def __enter__(self):
        if not self._active:
            raise RuntimeError("Private materialization cannot be reused")
        return self

    def __exit__(self, *args):
        self._active = False
        self._temporary.cleanup()


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with text *content*."""
    atomic_write_bytes(path, content.encode(encoding))


def durable_unlink(path: Path, *, missing_ok: bool = False) -> None:
    """Remove a file and flush its directory before a durable commit can follow."""
    try:
        path.unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise
    else:
        _fsync_directory(path.parent)


def sync_file_and_parents(path: Path, *, ancestor: Path) -> None:
    """Confirm a possibly interrupted replace/unlink before its durable receipt.

    Finding the desired bytes, or finding no file after deletion, does not prove
    that a previous directory sync succeeded. Flush the current file when present
    and all existing directories through the controlled ancestor on every retry.
    """
    relative = path.relative_to(ancestor)
    if not relative.parts or ".." in relative.parts:
        raise ValueError("Durability confirmation requires a strict contained path")
    if path.exists():
        if path.is_dir():
            _fsync_directory(path)
        else:
            with path.open("rb") as reader:
                os.fsync(reader.fileno())
    for directory in path.parents:
        if directory.is_dir():
            _fsync_directory(directory)
        if directory == ancestor:
            break


def prune_empty_parents(path: Path, *, stop: Path) -> None:
    """Remove only empty parents of a managed deletion, retaining unknown files."""
    import errno

    relative = path.relative_to(stop)
    if not relative.parts or ".." in relative.parts:
        raise ValueError("Empty-directory pruning requires a strict contained path")
    for directory in path.parents:
        if directory == stop:
            break
        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError as error:
            if error.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR}:
                break
            raise
        _fsync_directory(directory.parent)


def atomic_write_json(
    path: Path,
    data: object,
    *,
    ensure_ascii: bool = True,
    default=None,
) -> None:
    """Atomically replace *path* with formatted JSON."""
    atomic_write_text(
        path,
        json.dumps(data, indent=2, ensure_ascii=ensure_ascii, default=default) + "\n",
    )
