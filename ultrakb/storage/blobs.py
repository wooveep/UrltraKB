"""Immutable CAS payloads; business stores own reference commits."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ultrakb.locks import _fsync_directory, atomic_write_bytes
from ultrakb.storage.coordination import require_stage
from ultrakb.storage.database import Database


class BlobRetiring(RuntimeError):
    """Release this stage and retry after independent maintenance completes."""


@dataclass(frozen=True)
class Blob:
    digest: str
    byte_count: int


def blob_path(root: Path, digest: str) -> Path:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Invalid Blob digest")
    path = root / ".ultrakb/blobs/sha256" / digest[:2] / digest
    if any(part.is_symlink() for part in (path, *path.parents[:4])):
        raise ValueError("CAS content cannot follow symbolic links")
    return path


def _prepare_directory(root: Path, target: Path) -> None:
    for directory in reversed(target.parent.relative_to(root).parents):
        (root / directory).mkdir(exist_ok=True)
    target.parent.mkdir(exist_ok=True)
    # Newly created ancestor directory entries must precede the durable reference.
    for directory in (target.parent, *target.parent.parents):
        _fsync_directory(directory)
        if directory == root:
            break


def put_blob(db: Database, payload: bytes) -> Blob:
    require_stage(db)
    if db.read_only:
        raise PermissionError("Read-only storage cannot accept Blob content")
    if db.connection.in_transaction:
        raise RuntimeError("Blob I/O must precede the reference transaction")
    blob = Blob(hashlib.sha256(payload).hexdigest(), len(payload))
    row = db.connection.execute(
        "SELECT byte_count, state FROM ukb_blobs WHERE digest = ?", (blob.digest,)
    ).fetchone()
    if row and row[1] != "live":
        raise BlobRetiring("Blob retirement is incomplete; retry after maintenance")
    path = blob_path(db.root, blob.digest)
    if row or path.exists():
        if read_bytes(db, blob.digest, registered=bool(row)) != payload:
            raise ValueError("CAS content does not match its digest")
    if not row:
        # An unregistered final name may survive an interrupted directory sync.
        # Validate it above, then re-establish file and directory durability.
        _prepare_directory(db.root, path)
        atomic_write_bytes(path, payload)
    # A crash before this commit leaves only a recoverable orphan, never a ref.
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO ukb_blobs VALUES (?, ?, 'live') ON CONFLICT(digest) DO NOTHING",
            (blob.digest, blob.byte_count),
        )
    return blob


def read_bytes(db: Database, digest: str, *, registered: bool = True) -> bytes:
    require_stage(db)
    row = db.connection.execute(
        "SELECT byte_count, state FROM ukb_blobs WHERE digest = ?", (digest,)
    ).fetchone()
    if registered and (not row or row[1] != "live"):
        raise ValueError("Blob is missing or unavailable")
    path = blob_path(db.root, digest)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("CAS content cannot follow symbolic links")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest or row and len(payload) != row[0]:
        raise ValueError("CAS content failed integrity validation")
    return payload


def retain_blob(connection, owner_kind: str, owner_id: str, slot: str, blob: Blob) -> None:
    """Called only by the owning business store inside its record transaction."""
    if not connection.in_transaction:
        raise RuntimeError("A reference must commit with its owning business record")
    connection.execute(
        "INSERT INTO ukb_blob_refs VALUES (?, ?, ?, ?)",
        (owner_kind, owner_id, slot, blob.digest),
    )
