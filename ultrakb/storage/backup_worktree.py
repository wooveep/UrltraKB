"""Capture paused differences without accepting them; restore their applied baseline."""

import os
from pathlib import Path

from ultrakb.locks import durable_unlink, prune_empty_parents
from ultrakb.mutation import _copy_file_atomic

from .backup_state import BackupWikiChange, BackupWorktree, fingerprint, worktree_payload
from .blobs import blob_path
from .coordination import storage_stage
from .wiki_edit_store import WikiEditStore
from .wiki_paths import validate_file_tree
from .wiki_projection import ProjectionStore


def _working_files(projection):
    directory = projection.directory
    if directory.is_symlink():
        raise ValueError("Backup cannot follow working-copy symbolic links")
    files = {}
    for base, directories, names in os.walk(directory, followlinks=False):
        for name in (*directories, *names):
            path = Path(base) / name
            relative = path.relative_to(directory).as_posix()
            target = projection._target(relative)
            if target.is_dir():
                continue
            if not target.is_file():
                raise ValueError("Backup found an unsupported working-copy entry")
            files[relative] = fingerprint(target)
    return files


def capture_worktrees(db, destination, copy_verified):
    """Caller owns projection coordination across this and the online snapshot.

    An external editor need not be stopped to take a backup. Rechecking rejects
    observed concurrent changes; arbitrary direct writes do not form an atomic
    filesystem snapshot and remain subject to the external-editor protocol.
    """
    records = []
    rows = db.connection.execute(
        "SELECT view_id, edit_id FROM ukb_projection_state WHERE paused = 1 ORDER BY view_id"
    )
    for view_id, edit_id in rows:
        store = WikiEditStore(db.root, db.identity, view_id=view_id)
        edit = store._load(db, edit_id)
        actual = _working_files(store.projection)
        changes = []
        for path, (digest, size) in sorted(actual.items()):
            if edit.baseline.get(path) == digest:
                continue
            copy_verified(
                store.projection._target(path), worktree_payload(destination, edit_id, path), digest
            )
            changes.append(BackupWikiChange(path=path, digest=digest, byte_count=size))
        if _working_files(store.projection) != actual:
            raise ValueError("The paused working copy changed during backup; retry the backup")
        records.append(
            BackupWorktree(
                view_id=view_id,
                edit_id=edit_id,
                changes=tuple(changes),
                removed=tuple(sorted(edit.baseline.keys() - actual.keys())),
            )
        )
    return tuple(records)


def restore_worktrees(backup, destination, identity, manifest):
    with storage_stage(destination, identity, read_only=True) as db:
        pauses = db.connection.execute(
            "SELECT view_id, edit_id FROM ukb_projection_state WHERE paused = 1 ORDER BY view_id"
        ).fetchall()
        if pauses != [(item.view_id, item.edit_id) for item in manifest.worktrees]:
            raise ValueError("Backup paused differences do not match the database snapshot")
        # Applied receipts describe reconstructible copies, including views without a pause.
        rows = db.connection.execute(
            "SELECT view_id, path, digest FROM ukb_projection_applied "
            "WHERE digest IS NOT NULL ORDER BY view_id, path"
        )
        while batch := rows.fetchmany(128):
            for view_id, path, digest in batch:
                target = ProjectionStore(destination, identity, view_id=view_id)._target(path)
                _copy_file_atomic(blob_path(destination, digest), target)
        for item in manifest.worktrees:
            store = WikiEditStore(destination, identity, view_id=item.view_id)
            baseline = store._load(db, item.edit_id).baseline
            if not set(item.removed) <= baseline.keys():
                raise ValueError("Backup deleted a path outside its applied baseline")
            final = (baseline.keys() - set(item.removed)) | {c.path for c in item.changes}
            validate_file_tree(final)
            for path in sorted(item.removed, reverse=True):
                target = store.projection._target(path)
                durable_unlink(target, missing_ok=True)
                prune_empty_parents(target, stop=store.projection.directory)
            for change in item.changes:
                _copy_file_atomic(
                    worktree_payload(backup, item.edit_id, change.path),
                    store.projection._target(change.path),
                )
