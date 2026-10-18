"""A persistent whole-Wiki pause with the last confirmed per-file baseline."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from ultrakb.locks import file_write_lock

from .blobs import Blob, put_blob, read_bytes, retain_blob
from .coordination import storage_stage
from .database import DatabaseIdentity
from .navigation_store import RecoveryRequired
from .wiki_projection import (
    ProjectionConflict,
    ProjectionStore,
    _batches,
    _json,
    activate_resumption_plan,
    prepare_resumption_plan,
    projection_lock,
)


@dataclass(frozen=True)
class ExternalEdit:
    edit_id: str
    directory: Path
    baseline: dict[str, str]


@dataclass(frozen=True)
class EditDraft:
    edit: ExternalEdit
    files: dict[str, bytes]
    conflicts: tuple[str, ...] = ()


def finish_edit(connection, view_id, edit_id, publication_id, action):
    if action not in {"accepted", "discarded"}:
        raise ValueError("An external edit must be explicitly accepted or discarded")
    changed = connection.execute(
        "UPDATE ukb_projection_state SET paused = 0, edit_id = NULL "
        "WHERE view_id = ? AND paused = 1 AND edit_id = ? AND EXISTS "
        "(SELECT 1 FROM ukb_wiki_edits WHERE edit_id = ? AND view_id = ? AND state = 'paused')",
        (view_id, edit_id, edit_id, view_id),
    ).rowcount
    if changed != 1:
        raise ProjectionConflict("The external edit is no longer the active pause")
    connection.execute(
        "UPDATE ukb_wiki_edits SET state = ?, publication_id = ? WHERE edit_id = ?",
        (action, publication_id, edit_id),
    )


class WikiEditStore:
    def __init__(self, root: Path, identity: DatabaseIdentity, *, view_id="legacy"):
        self.root, self.identity, self.view_id = root.resolve(), identity, view_id
        self.projection = ProjectionStore(root, identity, view_id=view_id)

    def _load(self, db, edit_id):
        row = db.connection.execute(
            "SELECT baseline_manifest, baseline_generation, state FROM ukb_wiki_edits "
            "WHERE edit_id = ? AND view_id = ?",
            (edit_id, self.view_id),
        ).fetchone()
        if row is None or row[2] != "paused":
            raise ProjectionConflict("The external edit is no longer paused")
        view, generation, files = json.loads(read_bytes(db, row[0]))
        if view != self.view_id or generation != row[1]:
            raise RecoveryRequired("External edit baseline has a different identity")
        return ExternalEdit(edit_id, self.projection.directory, {p: d for p, d, _ in files})

    def capture(self, edit_id: str, *, editor_stopped: bool) -> EditDraft:
        if editor_stopped is not True:
            raise ValueError("Confirm external writes have stopped before accepting or discarding")
        with storage_stage(self.root, self.identity) as db, projection_lock(self.root):
            edit = self._load(db, edit_id)
            state = db.connection.execute(
                "SELECT paused, edit_id FROM ukb_projection_state WHERE view_id = ?",
                (self.view_id,),
            ).fetchone()
            if state != (1, edit_id):
                raise ProjectionConflict("The external edit is no longer the active pause")
            files, conflicts = {}, []
            if edit.directory.is_symlink():
                return EditDraft(edit, {}, (".",))
            # The editor has explicitly stopped. Direct writes outside this protocol
            # can still race the hash/replace boundary; they are not an atomic API.
            for path in sorted(edit.directory.rglob("*")):
                relative = path.relative_to(edit.directory).as_posix()
                try:
                    target = self.projection._target(relative)
                    if target.is_dir():
                        continue
                    if not target.is_file():
                        conflicts.append(relative)
                        continue
                    files[relative] = target.read_bytes()
                except (ValueError, OSError):
                    conflicts.append(relative)
            return EditDraft(edit, files, tuple(conflicts))

    def discard(self, edit_id, *, editor_stopped, discard_unknown=()):
        from .knowledge_store import KnowledgeStore

        if not isinstance(discard_unknown, tuple) or any(
            not isinstance(path, str) for path in discard_unknown
        ):
            raise ValueError("Explicit unknown paths must be a tuple of logical paths")
        with storage_stage(self.root, self.identity) as db, projection_lock(self.root):
            draft = self.capture(edit_id, editor_stopped=editor_stopped)
            if draft.conflicts:
                return None, draft.conflicts
            current = KnowledgeStore(self.root, self.identity).bind(view_id=self.view_id)
            known = draft.edit.baseline.keys() | current.files().keys()
            unknown = draft.files.keys() - known - set(discard_unknown)
            if unknown:
                return current.publication_id, tuple(sorted(unknown))
            expected = []
            for path, body in sorted(draft.files.items()):
                blob = put_blob(db, body)
                expected.append((path, blob.digest, blob.byte_count))
            targets = db.connection.execute(
                "SELECT path, digest, byte_count FROM ukb_revision_files "
                "WHERE revision_id = ? ORDER BY path",
                (current.publication.revision_id,),
            ).fetchall()
            plan_id = uuid.uuid4().hex
            prepare_resumption_plan(
                db, plan_id, current.publication.revision_id, self.view_id, targets, expected
            )
            with (
                file_write_lock(self.root / ".ultrakb/ingest.lock"),
                db.transaction() as connection,
            ):
                activate_resumption_plan(db, current.publication_id, plan_id)
                finish_edit(connection, self.view_id, edit_id, current.publication_id, "discarded")
            return current.publication_id, ()

    def begin(self) -> ExternalEdit:
        with storage_stage(self.root, self.identity) as db, projection_lock(self.root):
            state = db.connection.execute(
                "SELECT active_plan_id, target_generation, applied_epoch, paused, edit_id "
                "FROM ukb_projection_state WHERE view_id = ?",
                (self.view_id,),
            ).fetchone()
            if state is None or state[0] is None:
                raise RecoveryRequired("Publish before opening the external worktree")
            if state[3]:
                return self._load(db, state[4])
            files = db.connection.execute(
                "SELECT path, digest, byte_count FROM ukb_projection_applied "
                "WHERE view_id = ? AND digest IS NOT NULL ORDER BY path",
                (self.view_id,),
            ).fetchall()
            edit_id = uuid.uuid4().hex
            manifest = put_blob(db, _json((self.view_id, state[1], files)))
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO ukb_wiki_edits VALUES (?, ?, ?, ?, 'draft', NULL)",
                    (edit_id, self.view_id, manifest.digest, state[1]),
                )
                retain_blob(connection, "wiki-edit", edit_id, "manifest", manifest)
            references = {"manifest": manifest.digest}
            for batch in _batches(files):
                for (_, digest, size), _ in batch:
                    if len(read_bytes(db, digest)) != size:
                        raise RecoveryRequired("External edit baseline content is incomplete")
                with db.transaction() as connection:
                    for (_, digest, size), slot in batch:
                        retain_blob(connection, "wiki-edit", edit_id, slot, Blob(digest, size))
                        references[slot] = digest
            stored = dict(
                db.connection.execute(
                    "SELECT slot, digest FROM ukb_blob_refs "
                    "WHERE owner_kind = 'wiki-edit' AND owner_id = ?",
                    (edit_id,),
                ).fetchall()
            )
            if stored != references:
                raise RecoveryRequired("External edit baseline references are incomplete")
            with db.transaction() as connection:
                changed = connection.execute(
                    "UPDATE ukb_projection_state SET paused = 1, edit_id = ? "
                    "WHERE view_id = ? AND paused = 0 AND target_generation = ? "
                    "AND applied_epoch = ?",
                    (edit_id, self.view_id, state[1], state[2]),
                ).rowcount
                if changed != 1:
                    raise ProjectionConflict("Worktree changed before the pause was persisted")
                connection.execute(
                    "UPDATE ukb_wiki_edits SET state = 'paused' WHERE edit_id = ?",
                    (edit_id,),
                )
            return self._load(db, edit_id)
