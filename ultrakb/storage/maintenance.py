"""Explicit bounded reclamation and reference-closed online backups."""

import hashlib
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from ultrakb.locks import _fsync_directory, atomic_write_json, durable_unlink
from ultrakb.storage.blobs import blob_path
from ultrakb.storage.coordination import storage_stage, try_reclamation
from ultrakb.storage.database import (
    DatabaseIdentity,
    _activate_database,
    database_path,
    open_database,
)


@dataclass(frozen=True)
class CollectionPreview:
    candidate_count: int
    candidate_bytes: int
    protected_count: int
    protected_bytes: int
    has_more: bool
    candidate_trees: int = 0
    protected_trees: int = 0


@dataclass(frozen=True)
class CollectionResult:
    status: str
    deleted_count: int = 0
    deleted_bytes: int = 0
    trees_deleted: int = 0
    tree_rows_deleted: int = 0


def _limits(max_items, max_bytes):
    if type(max_items) is not int or not 1 <= max_items <= 1000:
        raise ValueError("A collection batch must contain 1 to 1000 candidates")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("A collection batch must have a positive byte budget")


def _candidates(db, max_items, max_bytes):
    rows = db.connection.execute(
        "SELECT digest, byte_count FROM ukb_blobs b WHERE NOT EXISTS "
        "(SELECT 1 FROM ukb_blob_refs r WHERE r.digest = b.digest) ORDER BY digest LIMIT ?",
        (max_items + 1,),
    ).fetchall()
    selected, total = [], 0
    for digest, byte_count in rows:
        if len(selected) == max_items or total + byte_count > max_bytes:
            break
        selected.append((digest, byte_count))
        total += byte_count
    return selected, len(selected) < len(rows)


def _copy_verified(source: Path, target: Path, digest: str):
    """Copy private backup bytes durably without hard-linking the live CAS."""
    if source.is_symlink() or source.parent.is_symlink():
        raise ValueError("Backup cannot follow Blob symbolic links")
    target.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    with source.open("rb") as reader, target.open("xb") as writer:
        while chunk := reader.read(1024 * 1024):
            hasher.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if hasher.hexdigest() != digest:
        raise ValueError("Backup Blob failed digest verification")


def _verify_snapshot(root: Path, *, expected=None, sealed_archive=False, verify_blobs=True):
    with open_database(
        root, expected=expected, read_only=True, sealed_archive=sealed_archive
    ) as db:
        if db.connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup database failed integrity verification")
        if db.connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Backup database has broken references")
        refs = db.connection.execute(
            "SELECT DISTINCT b.digest, b.byte_count FROM ukb_blob_refs r "
            "JOIN ukb_blobs b ON b.digest = r.digest ORDER BY b.digest"
        ).fetchall()
        if verify_blobs:
            for digest, byte_count in refs:
                path = blob_path(root, digest)
                hasher = hashlib.sha256()
                size = 0
                with path.open("rb") as reader:
                    while chunk := reader.read(1024 * 1024):
                        hasher.update(chunk)
                        size += len(chunk)
                if size != byte_count or hasher.hexdigest() != digest:
                    raise ValueError("Backup Blob failed reference-closure validation")
        return db.identity, refs


def _prune_snapshot_orphans(connection):
    """An export omits orphan files, so it must also omit their metadata.

    Only a private exported/restored snapshot calls this; the live store and
    all its durable references are unchanged. SQL deletion remains bounded.
    """
    while connection.execute(
        "DELETE FROM ukb_blobs WHERE digest IN (SELECT b.digest FROM ukb_blobs b "
        "WHERE NOT EXISTS (SELECT 1 FROM ukb_blob_refs r WHERE r.digest = b.digest) LIMIT 128)"
    ).rowcount:
        pass


def _publish_directory(temporary: Path, destination: Path):
    # Flush bottom-up, including newly created shard directories, before rename.
    for directory, _, _ in os.walk(temporary, topdown=False):
        _fsync_directory(Path(directory))
    if destination.exists():
        raise FileExistsError(destination)
    os.rename(temporary, destination)
    _fsync_directory(destination.parent)


class Maintenance:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def preview(self, *, max_items: int = 128, max_bytes: int = 64 * 1024 * 1024):
        _limits(max_items, max_bytes)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            from .tree_reclamation import tree_candidates

            trees = tree_candidates(db, max_items + 1)
            tree_count = min(len(trees), max_items)
            rows, has_more = _candidates(db, max_items - tree_count, max_bytes)
            protected_trees = db.connection.execute(
                "SELECT count(*) FROM ukb_tree_builds b WHERE EXISTS "
                "(SELECT 1 FROM ukb_tree_refs r WHERE r.tree_id = b.tree_id) OR EXISTS "
                "(SELECT 1 FROM ukb_index_revisions i WHERE i.tree_id = b.tree_id)"
            ).fetchone()[0]
            protected = db.connection.execute(
                "SELECT count(*), coalesce(sum(byte_count), 0) FROM ukb_blobs b WHERE EXISTS "
                "(SELECT 1 FROM ukb_blob_refs r WHERE r.digest = b.digest)"
            ).fetchone()
            return CollectionPreview(
                len(rows),
                sum(size for _, size in rows),
                protected[0],
                protected[1],
                has_more or len(trees) > max_items,
                tree_count,
                protected_trees,
            )

    def collect(self, *, max_items: int = 128, max_bytes: int = 64 * 1024 * 1024):
        _limits(max_items, max_bytes)
        with try_reclamation(self.root, self.identity) as db:
            if db is None:
                return CollectionResult("busy")
            from .tree_reclamation import reclaim_trees

            trees_deleted, tree_rows_deleted = reclaim_trees(db, max_items=max_items)
            # A preview's candidates are deliberately never accepted as deletion authority.
            blob_limit = 0 if tree_rows_deleted and not trees_deleted else max_items - trees_deleted
            rows, _ = _candidates(db, blob_limit, max_bytes)
            deleted_count = deleted_bytes = 0
            for digest, byte_count in rows:
                with db.transaction() as connection:
                    updated = connection.execute(
                        "UPDATE ukb_blobs SET state = 'retiring' WHERE digest = ? AND NOT EXISTS "
                        "(SELECT 1 FROM ukb_blob_refs WHERE digest = ?)",
                        (digest, digest),
                    ).rowcount
                if not updated:
                    continue
                # An interrupted unlink leaves retiring durable; a later batch resumes it.
                durable_unlink(blob_path(self.root, digest), missing_ok=True)
                with db.transaction() as connection:
                    connection.execute(
                        "DELETE FROM ukb_blobs WHERE digest = ? AND state = 'retiring' "
                        "AND NOT EXISTS (SELECT 1 FROM ukb_blob_refs WHERE digest = ?)",
                        (digest, digest),
                    )
                deleted_count += 1
                deleted_bytes += byte_count
            return CollectionResult(
                "completed", deleted_count, deleted_bytes, trees_deleted, tree_rows_deleted
            )

    def backup(self, destination: Path) -> DatabaseIdentity:
        destination = destination.resolve()
        if destination.exists() or destination.is_relative_to(self.root):
            raise ValueError("Backup requires a new directory outside the knowledge base")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".ultrakb-backup-", dir=destination.parent))
        try:
            state = temporary / ".ultrakb"
            state.mkdir()
            # Protection begins before the online snapshot and lasts through Blob copies.
            with storage_stage(self.root, self.identity, read_only=True) as db:
                snapshot = sqlite3.connect(state / "kb.sqlite", isolation_level=None)
                try:
                    db.connection.backup(snapshot)
                    snapshot.execute("PRAGMA synchronous = FULL")
                    _prune_snapshot_orphans(snapshot)
                    snapshot.execute("PRAGMA journal_mode = DELETE")
                finally:
                    snapshot.close()
                _, refs = _verify_snapshot(
                    temporary, expected=self.identity, sealed_archive=True, verify_blobs=False
                )
                for digest, _ in refs:
                    _copy_verified(
                        blob_path(self.root, digest), blob_path(temporary, digest), digest
                    )
                _verify_snapshot(temporary, expected=self.identity, sealed_archive=True)
                atomic_write_json(
                    temporary / "backup.json",
                    {
                        "format": "ultrakb.backup",
                        "version": 1,
                        "kb_id": self.identity.kb_id,
                        "generation": self.identity.generation,
                        "referenced_blobs": len(refs),
                        # These development stores do not yet capture adapter configuration,
                        # paused worktree diffs or external unconfirmed receipts.
                        "coverage": {
                            "committed_database": "complete",
                            "referenced_blobs": "complete",
                            "configuration": "not_captured",
                            "worktree": "not_captured",
                            "external_receipts": "not_captured",
                        },
                    },
                )
                with database_path(temporary).open("rb") as handle:
                    os.fsync(handle.fileno())
            _publish_directory(temporary, destination)
            return self.identity
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    @staticmethod
    def restore(backup: Path, destination: Path) -> DatabaseIdentity:
        identity, refs = _verify_snapshot(backup, sealed_archive=True)
        destination = destination.resolve()
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".ultrakb-restore-", dir=destination.parent))
        try:
            (temporary / ".ultrakb").mkdir()
            with open_database(
                backup, expected=identity, read_only=True, sealed_archive=True
            ) as db:
                snapshot = sqlite3.connect(database_path(temporary), isolation_level=None)
                try:
                    db.connection.backup(snapshot)
                    snapshot.execute("PRAGMA synchronous = FULL")
                    _prune_snapshot_orphans(snapshot)
                finally:
                    snapshot.close()
            _activate_database(temporary, identity)
            for digest, _ in refs:
                _copy_verified(blob_path(backup, digest), blob_path(temporary, digest), digest)
            with open_database(temporary, expected=identity) as db:
                generation = uuid.uuid4().hex
                with db.transaction() as connection:
                    connection.execute("UPDATE ukb_meta SET generation = ?", (generation,))
            restored = DatabaseIdentity(identity.kb_id, generation)
            _verify_snapshot(temporary, expected=restored)
            with database_path(temporary).open("rb") as handle:
                os.fsync(handle.fileno())
            _publish_directory(temporary, destination)
            return restored
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
