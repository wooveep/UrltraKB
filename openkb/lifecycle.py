"""Directory lifetime protection outside the directory that may be deleted.

Every KB/session/repair lease holds shared access. Delete and creation hold
exclusive access, so Windows can close ingest.lock without exposing a gap.
The small per-user records survive directory deletion and reject old waiters.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from openkb.locks import _check_wait, _Lease, atomic_write_json, file_write_lock_held
from openkb.mutation import RecoveryRequired


class KnowledgeBaseRemoved(FileNotFoundError):
    """This operation belongs to a directory generation that was removed."""


class KnowledgeBaseIncomplete(ValueError):
    """Recovery left no completed KB on which ordinary work could operate."""


@dataclass(frozen=True)
class LifecycleState:
    generation: str = "initial"
    status: str = "active"
    directory: tuple[int, int] | None = None


def _paths(root: Path) -> tuple[Path, Path]:
    # Independent of KB/global config overrides and program installation paths.
    # All local entry points for this OS account use the same stable sidecar.
    name = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    directory = Path.home() / ".openkb/kb-lifecycle"
    return directory / f"{name}.lock", directory / f"{name}.json"


def read_state(kb_dir: Path) -> LifecycleState:
    return _read_state(kb_dir.resolve())


def _read_state(root: Path) -> LifecycleState:
    _, path = _paths(root)
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return LifecycleState()
    try:
        value = json.loads(text)
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
            or value.get("root") != str(root)
        ):
            raise ValueError("Invalid lifecycle identity")
        generation, status, directory = value["generation"], value["status"], value["directory"]
        if not isinstance(generation, str) or not generation:
            raise ValueError("Invalid directory generation")
        if status not in {"active", "deleting", "absent"}:
            raise ValueError("Invalid directory lifecycle")
        if directory is not None and (
            not isinstance(directory, list)
            or len(directory) != 2
            or any(type(item) is not int for item in directory)
        ):
            raise ValueError("Invalid directory identity")
        return LifecycleState(generation, status, tuple(directory) if directory else None)
    except (ValueError, KeyError, TypeError) as exc:
        raise RecoveryRequired(f"Knowledge-base lifecycle record needs inspection: {root}") from exc


def write_state(root: Path, state: LifecycleState) -> None:
    root = root.resolve()
    lock, path = _paths(root)
    if not file_write_lock_held(lock):
        raise RuntimeError("Directory lifecycle changes require exclusive access")
    atomic_write_json(
        path,
        {
            "version": 1,
            "root": str(root),
            "generation": state.generation,
            "status": state.status,
            "directory": state.directory,
        },
    )


def directory_identity(root: Path) -> tuple[int, int] | None:
    try:
        info = root.stat()
        return info.st_dev, info.st_ino
    except FileNotFoundError:
        return None


_expected: ContextVar[tuple[Path, str] | None] = ContextVar("kb_generation", default=None)


def _binding(root: Path, state: LifecycleState) -> str:
    # Include the observed directory, not only the last managed creation. A
    # copied/replaced KB can be opened afresh, but queued work keeps its old
    # physical identity even when no OpenKB deletion changed the sidecar.
    identity = (str(root), state.generation, directory_identity(root))
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


@contextmanager
def expected_generation(kb_dir: Path, generation: str | None):
    root = kb_dir.resolve()
    if generation is not None:
        _validate(root, generation)
    token = _expected.set((root, generation) if generation is not None else None)
    try:
        yield
    finally:
        _expected.reset(token)


def _generation(root: Path) -> str:
    expected = _expected.get()
    if expected:
        _validate(*expected)
        if expected[0] == root:
            return expected[1]
    return _binding(root, read_state(root))


def current_generation(kb_dir: Path) -> str:
    """Bind newly submitted work without waiting on filesystem execution."""
    root = kb_dir.resolve()
    generation = _binding(root, read_state(root))
    _validate(root, generation)
    return generation


def validate_execution(kb_dir: Path) -> None:
    """Recovery may have rolled back an incomplete initialization."""
    root = kb_dir.resolve()
    if file_write_lock_held(_paths(root)[0]):
        return  # Explicit creation/deletion owns the whole directory lifecycle.
    state = read_state(root)
    if state.generation != "initial" and not (root / ".openkb/config.yaml").is_file():
        raise KnowledgeBaseIncomplete(
            f"Knowledge base not initialized; diagnose or initialize it: {root}"
        )


def begin_creation(kb_dir: Path) -> None:
    """Called after creation eligibility/recovery, before any new seed writes."""
    root = kb_dir.resolve()
    write_state(root, LifecycleState(uuid.uuid4().hex, "active", directory_identity(root)))


def _validate(root: Path, generation: str) -> None:
    current = read_state(root)
    if _binding(root, current) != generation:
        raise KnowledgeBaseRemoved(f"Knowledge base was removed or recreated; reopen it: {root}")
    if current.status == "deleting":
        raise RecoveryRequired(f"Knowledge-base deletion is incomplete; finish deletion: {root}")
    if current.status == "absent" and not (root / ".openkb/config.yaml").is_file():
        raise KnowledgeBaseRemoved(f"Knowledge base was deleted: {root}")


@contextmanager
def read_lifecycle(kb_dir: Path, *, cancelled=None, on_wait=None, deadline=None):
    root = kb_dir.resolve()
    generation = _generation(root)
    lease = _Lease(_paths(root)[0], False)
    notified = False
    token = None
    try:
        while True:
            _check_wait(cancelled, deadline)
            if lease.try_acquire():
                break
            if on_wait and not notified:
                on_wait()
                notified = True
            time.sleep(0.05)
        if not file_write_lock_held(_paths(root)[0]):
            _validate(root, generation)
            token = _expected.set((root, generation))
        _check_wait(cancelled, deadline)
        yield
    finally:
        if token is not None:
            _expected.reset(token)
        lease.release()


@asynccontextmanager
async def async_read_lifecycle(kb_dir: Path, *, cancelled=None, on_wait=None, deadline=None):
    root = kb_dir.resolve()
    generation = _generation(root)
    lease = _Lease(_paths(root)[0], False)
    notified = False
    token = None
    try:
        while True:
            _check_wait(cancelled, deadline)
            if lease.try_acquire():
                break
            if on_wait and not notified:
                on_wait()
                notified = True
            await asyncio.sleep(0.05)
        if not file_write_lock_held(_paths(root)[0]):
            _validate(root, generation)
            token = _expected.set((root, generation))
        _check_wait(cancelled, deadline)
        yield
    finally:
        if token is not None:
            _expected.reset(token)
        lease.release()


@contextmanager
def exclusive_lifecycle(kb_dir: Path, *, cancelled=None, on_wait=None):
    root = kb_dir.resolve()
    lease = _Lease(_paths(root)[0], True)
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
        yield lease.first
    finally:
        lease.release()


@contextmanager
def creation_lifecycle(kb_dir: Path, **wait_options):
    root = kb_dir.resolve()
    with exclusive_lifecycle(root, **wait_options) as first:
        if not first:
            yield
            return
        previous = read_state(root)
        if previous.status == "deleting":
            raise RecoveryRequired(f"Finish deleting the previous knowledge base first: {root}")
        yield


def deletion_target(kb_dir: Path) -> Path:
    """Never let an unfinished removal follow a replacement symlink/junction."""
    requested = kb_dir.expanduser().absolute()
    root = requested.resolve()
    if requested.is_symlink():
        raise ValueError("Refusing to delete a knowledge base through a symbolic link")
    if root != requested and _read_state(requested).status == "deleting":
        raise ValueError("Refusing to resume deletion of a replaced directory")
    return root


def deletion_binding(kb_dir: Path) -> str:
    """Bind a confirmed removal, including an explicit partial-removal retry."""
    root = deletion_target(kb_dir)
    return _binding(root, read_state(root))


def has_pending_deletion(kb_dir: Path) -> bool:
    """Check the original path as well as its current canonical target."""
    requested = kb_dir.expanduser().absolute()
    return _read_state(requested).status == "deleting" or read_state(requested).status == "deleting"


def registered_path(kb_dir: Path) -> Path:
    """Keep unfinished removal entries attached to the directory originally named."""
    requested = kb_dir.expanduser().absolute()
    try:
        if _read_state(requested).status == "deleting":
            return requested
    except RecoveryRequired:
        return requested  # Keep the entry available for diagnosis of its own record.
    return requested.resolve()
