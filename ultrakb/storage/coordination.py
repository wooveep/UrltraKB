"""A storage stage protects real access; model waits hold no stage lease."""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ultrakb.lifecycle import read_lifecycle
from ultrakb.locks import _check_wait, _Lease, _owner, file_lock_held
from ultrakb.storage.database import Database, DatabaseIdentity, open_database


@contextmanager
def storage_stage(
    root: Path, expected: DatabaseIdentity, *, read_only: bool = False, cancelled=None
) -> Iterator[Database]:
    import time

    root = root.resolve()
    with read_lifecycle(root, cancelled=cancelled, database_identity=expected):
        lease = _Lease(root / ".ultrakb/locks/reclaim.lock", False)
        try:
            while not lease.try_acquire():
                _check_wait(cancelled, None)
                time.sleep(0.01)
            _check_wait(cancelled, None)
            with open_database(root, expected=expected, read_only=read_only) as db:
                db.storage_lease = lease
                db.storage_owner = _owner()
                yield db
        finally:
            lease.release()


def require_stage(db: Database) -> None:
    if not db.storage_lease or not db.storage_lease.acquired or db.storage_owner != _owner():
        raise RuntimeError("Content access requires this thread/task's storage stage")


@contextmanager
def try_reclamation(root: Path, expected: DatabaseIdentity) -> Iterator[Database | None]:
    """Never wait for or upgrade a reader; a busy result performs no deletion."""
    root = root.resolve()
    with read_lifecycle(root, database_identity=expected):
        lease = _Lease(root / ".ultrakb/locks/reclaim.lock", True)
        try:
            if file_lock_held(lease.path):
                yield None
                return
            if not lease.try_acquire():
                yield None
                return
            with open_database(root, expected=expected) as db:
                db.storage_lease = lease
                db.storage_owner = _owner()
                yield db
        finally:
            lease.release()
