"""Sealed file synchronization is recoverable after the master publication commits."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ultrakb.locks import (
    atomic_write_bytes,
    durable_unlink,
    file_write_lock,
    prune_empty_parents,
    sync_file_and_parents,
)

from .blobs import Blob, put_blob, read_bytes, retain_blob
from .coordination import storage_stage
from .database import DatabaseIdentity
from .navigation_store import RecoveryRequired
from .wiki_paths import wiki_path

_UNHASHED = object()


class ProjectionConflict(ValueError):
    """Prepare a new plan from the last confirmed worktree baseline."""


def projection_lock(root):
    return file_write_lock(root / ".ultrakb/wiki-projection.lock")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _slot(path):
    return hashlib.sha256(path.encode()).hexdigest()


def _batches(items):
    batch, encoded = [], 0
    for item in items:
        slot = _slot(item[0])
        size = len(_json(item)) + len(_json(("expected:" + slot, "target:" + slot))) + 512
        if size > 64 * 1024:
            raise ValueError("Synchronization item exceeds the encoded storage limit")
        if batch and (len(batch) >= 42 or encoded + size > 256 * 1024):
            yield batch
            batch, encoded = [], 0
        batch.append((item, slot))
        encoded += size
    if batch:
        yield batch


def prepare_sync_plan(db, plan_id, revision_id, view_id, files):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO ukb_projection_state VALUES (?, NULL, 0, 0, 0, NULL) "
            "ON CONFLICT(view_id) DO NOTHING",
            (view_id,),
        )
    captured = db.connection.execute(
        "SELECT target_generation, applied_epoch FROM ukb_projection_state WHERE view_id = ?",
        (view_id,),
    ).fetchone()
    applied = {
        row[0]: row[1:]
        for row in db.connection.execute(
            "SELECT path, digest, byte_count FROM ukb_projection_applied "
            "WHERE view_id = ? AND digest IS NOT NULL ORDER BY path",
            (view_id,),
        ).fetchall()
    }
    for path in pending_paths(db, view_id):
        applied.setdefault(path, (None, 0))
    _prepare_plan(db, plan_id, revision_id, view_id, files, captured, applied, bind_revision=True)


def prepare_resumption_plan(db, plan_id, revision_id, view_id, files, expected_files):
    captured = db.connection.execute(
        "SELECT target_generation, applied_epoch FROM ukb_projection_state "
        "WHERE view_id = ? AND paused = 1",
        (view_id,),
    ).fetchone()
    if captured is None:
        raise ProjectionConflict("Resuming external edits requires the persistent pause")
    applied = {
        row[0]: (None, 0)
        for row in db.connection.execute(
            "SELECT path FROM ukb_projection_applied WHERE view_id = ? AND digest IS NOT NULL",
            (view_id,),
        ).fetchall()
    }
    applied.update({path: (digest, size) for path, digest, size in expected_files})
    for path in pending_paths(db, view_id):
        applied.setdefault(path, (None, 0))
    _prepare_plan(db, plan_id, revision_id, view_id, files, captured, applied, bind_revision=False)


def _prepare_plan(db, plan_id, revision_id, view_id, files, captured, applied, *, bind_revision):
    kind = "publication" if bind_revision else "resumption"
    targets = {path: (digest, size) for path, digest, size in files}
    items = [
        (path, *applied.get(path, (None, 0)), *targets.get(path, (None, 0)))
        for path in sorted(applied.keys() | targets.keys())
    ]
    manifest = put_blob(db, _json((kind, revision_id, view_id, captured, items)))
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO ukb_sync_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'draft')",
            (
                plan_id,
                revision_id,
                view_id,
                *captured,
                manifest.digest,
                len(items),
                len(items),
                kind,
            ),
        )
        retain_blob(connection, "sync-plan", plan_id, "manifest", manifest)
    for batch in _batches(items):
        with db.transaction() as connection:
            for item, slot in batch:
                connection.execute(
                    "INSERT INTO ukb_sync_items VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                    (plan_id, *item),
                )
                for name, digest, size in (
                    ("expected", item[1], item[2]),
                    ("target", item[3], item[4]),
                ):
                    if digest is not None:
                        retain_blob(
                            connection, "sync-plan", plan_id, name + ":" + slot, Blob(digest, size)
                        )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_sync_plans SET state = 'sealed' WHERE plan_id = ? AND state = 'draft'",
            (plan_id,),
        )
    rows = db.connection.execute(
        "SELECT path, expected_digest, expected_bytes, target_digest, target_bytes "
        "FROM ukb_sync_items WHERE plan_id = ? AND state = 'pending' ORDER BY path",
        (plan_id,),
    ).fetchall()
    if _json((kind, revision_id, view_id, captured, rows)) != read_bytes(db, manifest.digest):
        raise ValueError("Sealed synchronization plan does not match its complete manifest")
    expected_refs = {"manifest": (manifest.digest, manifest.byte_count, "live")}
    for path, previous, previous_size, target, target_size in rows:
        slot = _slot(path)
        if previous is not None:
            expected_refs["expected:" + slot] = (previous, previous_size, "live")
        if target is not None:
            expected_refs["target:" + slot] = (target, target_size, "live")
    # These contents were validated before retaining their owning records. Verify
    # exact reference closure without rereading every unchanged source or image.
    references = {
        slot: (digest, size, state)
        for slot, digest, size, state in db.connection.execute(
            "SELECT r.slot, r.digest, b.byte_count, b.state FROM ukb_blob_refs r "
            "LEFT JOIN ukb_blobs b ON b.digest = r.digest "
            "WHERE r.owner_kind = 'sync-plan' AND r.owner_id = ?",
            (plan_id,),
        ).fetchall()
    }
    if references != expected_refs:
        raise ValueError("Synchronization plan content references are incomplete")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_sync_plans SET state = 'ready' WHERE plan_id = ? AND state = 'sealed'",
            (plan_id,),
        )
        if bind_revision:
            connection.execute(
                "INSERT INTO ukb_revision_sync VALUES (?, ?)", (revision_id, plan_id)
            )


def pending_paths(db, view_id):
    """Include unconfirmed creations and explicit discard deletions in the next plan."""
    return tuple(
        row[0]
        for row in db.connection.execute(
            "SELECT DISTINCT i.path FROM ukb_sync_plans p JOIN ukb_sync_items i "
            "ON i.plan_id = p.plan_id LEFT JOIN ukb_projection_applied a "
            "ON a.view_id = p.view_id AND a.path = i.path WHERE p.view_id = ? "
            "AND p.state IN ('active', 'superseded') AND i.state = 'pending' "
            "AND p.base_generation >= COALESCE(a.generation, 0)",
            (view_id,),
        ).fetchall()
    )


def require_sync_plan(db, revision_id, plan_id):
    row = db.connection.execute(
        "SELECT p.view_id, p.base_generation, p.applied_epoch, p.remaining_count "
        "FROM ukb_revision_sync s JOIN ukb_sync_plans p ON p.plan_id = s.plan_id "
        "AND p.revision_id = s.revision_id WHERE s.revision_id = ? AND s.plan_id = ? "
        "AND p.state = 'ready'",
        (revision_id, plan_id),
    ).fetchone()
    if row is None:
        raise RecoveryRequired("Knowledge requires its exact sealed synchronization plan")
    return row


def activate_sync_plan(db, revision_id, plan_id):
    _activate_plan(db, plan_id, require_sync_plan(db, revision_id, plan_id))


def activate_resumption_plan(db, publication_id, plan_id):
    row = db.connection.execute(
        "SELECT s.view_id, s.base_generation, s.applied_epoch, s.remaining_count "
        "FROM ukb_sync_plans s JOIN ukb_publications p "
        "ON p.revision_id = s.revision_id AND p.view_id = s.view_id "
        "JOIN ukb_heads h ON h.view_id = p.view_id AND h.publication_id = p.publication_id "
        "WHERE s.plan_id = ? AND p.publication_id = ? AND s.state = 'ready'",
        (plan_id, publication_id),
    ).fetchone()
    if row is None:
        raise ProjectionConflict("The resumption plan must target the current publication")
    _activate_plan(db, plan_id, row)


def _activate_plan(db, plan_id, prepared):
    if not db.connection.in_transaction:
        raise ValueError("Synchronization activation belongs to the publication transaction")
    view_id, generation, epoch, remaining = prepared
    state = db.connection.execute(
        "SELECT active_plan_id, target_generation, applied_epoch FROM ukb_projection_state "
        "WHERE view_id = ?",
        (view_id,),
    ).fetchone()
    if state is None or state[1:] != (generation, epoch):
        raise ProjectionConflict(
            "The confirmed worktree baseline changed; prepare the candidate again"
        )
    if state[0]:
        db.connection.execute(
            "UPDATE ukb_sync_plans SET state = 'superseded' WHERE plan_id = ?", (state[0],)
        )
    db.connection.execute(
        "UPDATE ukb_sync_plans SET state = ? WHERE plan_id = ?",
        ("active" if remaining else "completed", plan_id),
    )
    db.connection.execute(
        "UPDATE ukb_projection_state SET active_plan_id = ?, target_generation = ? "
        "WHERE view_id = ?",
        (plan_id, generation + 1, view_id),
    )


@dataclass(frozen=True)
class ProjectionResult:
    status: str
    plan_id: str | None
    applied_count: int = 0
    pending_count: int = 0
    conflict_count: int = 0
    error_code: str | None = None


class ProjectionStore:
    def __init__(self, root: Path, identity: DatabaseIdentity, *, view_id="legacy"):
        self.root, self.identity, self.view_id = root.resolve(), identity, view_id

    @property
    def directory(self):
        if self.view_id == "legacy":
            return self.root / "wiki"
        return self.root / ".ultrakb/wiki-projections" / _slot(self.view_id) / "wiki"

    def _target(self, path):
        wiki_path(path)
        target = self.directory.joinpath(*path.split("/"))
        for part in (target, *target.parents):
            if part.is_symlink():
                raise ValueError("Working-copy paths cannot follow symlinks")
            if part == self.root:
                break
        return target

    def sync(self, *, plan_id=None, max_items=63, max_bytes=256 * 1024 * 1024) -> ProjectionResult:
        if (
            type(max_items) is not int
            or not 1 <= max_items <= 128
            or type(max_bytes) is not int
            or max_bytes < 1
        ):
            raise ValueError("Synchronization requires bounded positive row and byte limits")
        owner = _slot(self.view_id)
        with storage_stage(self.root, self.identity) as db, projection_lock(self.root):
            state = db.connection.execute(
                "SELECT active_plan_id, target_generation, paused FROM ukb_projection_state "
                "WHERE view_id = ?",
                (self.view_id,),
            ).fetchone()
            if state is None or state[0] is None:
                raise RecoveryRequired("Working-copy projection requires explicit preparation")
            if plan_id is not None and plan_id != state[0]:
                return ProjectionResult("obsolete", plan_id)
            plan_id, generation, paused = state
            if paused:
                return ProjectionResult("paused", plan_id)
            rows = db.connection.execute(
                "SELECT path, expected_digest, expected_bytes, target_digest, target_bytes "
                "FROM ukb_sync_items WHERE plan_id = ? AND state = 'pending' "
                "ORDER BY (target_digest IS NOT NULL), path DESC LIMIT ?",
                (plan_id, max_items),
            ).fetchall()
            applied = spent = 0
            error_code = None
            for path, previous, previous_bytes, target, target_bytes in rows:
                slot = _slot(path)
                try:
                    try:
                        file = self._target(path)
                    except ValueError:
                        self._conflict(db, plan_id, path)
                        continue
                    actual = None
                    actual_bytes = 0
                    directory = file.is_dir()
                    if directory and previous is None and target is None:
                        pass  # The logical file is absent; retain the directory and its contents.
                    elif file.exists():
                        if not file.is_file():
                            self._conflict(db, plan_id, path)
                            continue
                        actual_bytes = file.stat().st_size
                        if actual_bytes not in {
                            previous_bytes,
                            target_bytes,
                        } and not self._can_recover(db, path, previous, generation, actual_bytes):
                            actual = "conflict"
                        else:
                            cost = actual_bytes + 2 * target_bytes
                            if spent + cost > max_bytes:
                                error_code = "byte_budget"
                                break
                            spent += cost
                            hasher = hashlib.sha256()
                            with file.open("rb") as reader:
                                while chunk := reader.read(1024 * 1024):
                                    hasher.update(chunk)
                            actual = hasher.hexdigest()
                    elif spent + 2 * target_bytes > max_bytes:
                        error_code = "byte_budget"
                        break
                    else:
                        spent += 2 * target_bytes
                    if (
                        actual != target
                        and actual != previous
                        and not self._can_recover(
                            db, path, previous, generation, actual_bytes, actual
                        )
                    ):
                        self._conflict(db, plan_id, path)
                        continue
                    if target is None:
                        if actual is not None:
                            durable_unlink(file, missing_ok=True)
                        if not directory:
                            prune_empty_parents(file, stop=self.directory)
                    elif actual != target:
                        atomic_write_bytes(file, read_bytes(db, target))
                    confirmed = db.connection.execute(
                        "SELECT digest, byte_count FROM ukb_projection_applied "
                        "WHERE view_id = ? AND path = ?",
                        (self.view_id, path),
                    ).fetchone()
                    if (
                        actual != target
                        or previous != target
                        or confirmed != (target, target_bytes)
                    ):
                        # A previous replace/unlink may have failed its directory sync
                        # even though its current bytes already match the target.
                        sync_file_and_parents(file, ancestor=self.root)
                    self._confirm(db, owner, slot, plan_id, generation, path, target, target_bytes)
                    applied += 1
                except (OSError, sqlite3.Error) as error:
                    error_code = type(error).__name__
                    break
            remaining, conflicts = db.connection.execute(
                "SELECT remaining_count, conflict_count FROM ukb_sync_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            return ProjectionResult(
                "conflict" if conflicts else "pending" if remaining else "synced",
                plan_id,
                applied,
                remaining,
                conflicts,
                error_code,
            )

    def _can_recover(self, db, path, previous, generation, size, digest=_UNHASHED):
        # A superseded plan may have replaced/unlinked the file and crashed before
        # confirming it. Recognize only an actual former active target newer than
        # the last applied receipt, for this exact path and unchanged baseline.
        # These pending predecessor references remain roots until current sync
        # completes; arbitrary files and never-activated candidates cannot qualify.
        query = (
            "SELECT 1 FROM ukb_sync_items i JOIN ukb_sync_plans p ON p.plan_id = i.plan_id "
            "LEFT JOIN ukb_projection_applied a ON a.view_id = p.view_id AND a.path = i.path "
            "WHERE i.path = ? AND i.state = 'pending' "
            "AND p.view_id = ? AND p.state = 'superseded' "
            "AND p.base_generation >= COALESCE(a.generation, 0) AND p.base_generation < ? "
            "AND a.digest IS ? "
        )
        parameters = (path, self.view_id, generation - 1, previous)
        target = "i.target_bytes = ? AND (p.kind = 'resumption' OR i.expected_digest IS ?) "
        expected = "p.kind = 'resumption' AND i.expected_bytes = ? "
        target_parameters, expected_parameters = (size, previous), (size,)
        if digest is not _UNHASHED:
            target += "AND i.target_digest IS ? "
            expected += "AND i.expected_digest IS ? "
            target_parameters += (digest,)
            expected_parameters += (digest,)
        query += "AND ((" + target + ") OR (" + expected + ")) LIMIT 1"
        return (
            db.connection.execute(
                query, (*parameters, *target_parameters, *expected_parameters)
            ).fetchone()
            is not None
        )

    def _conflict(self, db, plan_id, path):
        with db.transaction() as connection:
            if connection.execute(
                "UPDATE ukb_sync_items SET state = 'conflict' WHERE plan_id = ? AND path = ? "
                "AND state = 'pending'",
                (plan_id, path),
            ).rowcount:
                connection.execute(
                    "UPDATE ukb_sync_plans SET conflict_count = conflict_count + 1 "
                    "WHERE plan_id = ?",
                    (plan_id,),
                )

    def _confirm(self, db, owner, slot, plan_id, generation, path, digest, size):
        with db.transaction() as connection:
            state = connection.execute(
                "SELECT active_plan_id, target_generation, paused FROM ukb_projection_state "
                "WHERE view_id = ?",
                (self.view_id,),
            ).fetchone()
            if state != (plan_id, generation, 0):
                raise ProjectionConflict("Working-copy target changed before confirmation")
            previous = connection.execute(
                "SELECT digest, byte_count FROM ukb_projection_applied "
                "WHERE view_id = ? AND path = ?",
                (self.view_id, path),
            ).fetchone()
            if previous != (digest, size):
                connection.execute(
                    "DELETE FROM ukb_blob_refs WHERE owner_kind = 'wiki-applied' "
                    "AND owner_id = ? AND slot = ?",
                    (owner, slot),
                )
                if digest is not None:
                    retain_blob(connection, "wiki-applied", owner, slot, Blob(digest, size))
                connection.execute(
                    "UPDATE ukb_projection_state SET applied_epoch = applied_epoch + 1 "
                    "WHERE view_id = ?",
                    (self.view_id,),
                )
            connection.execute(
                "INSERT INTO ukb_projection_applied VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(view_id, path) DO UPDATE SET digest = excluded.digest, "
                "byte_count = excluded.byte_count, generation = excluded.generation",
                (self.view_id, path, digest, size, generation),
            )
            changed = connection.execute(
                "UPDATE ukb_sync_items SET state = 'applied' "
                "WHERE plan_id = ? AND path = ? AND state = 'pending'",
                (plan_id, path),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE ukb_sync_plans SET remaining_count = remaining_count - 1, "
                    "state = CASE WHEN remaining_count = 1 THEN 'completed' ELSE state END "
                    "WHERE plan_id = ?",
                    (plan_id,),
                )
