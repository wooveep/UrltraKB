"""Shared use cases for controlled external editing of a new-format worktree.

These development entry points stay separate until all new-format application
writers and readers are integrated. Calling begin again resumes the same pause.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict
from ultrakb.storage.wiki_edit_store import ExternalEdit, WikiEditStore
from ultrakb.storage.wiki_projection import ProjectionConflict, projection_lock


@dataclass(frozen=True)
class EditOutcome:
    status: str
    publication_id: str | None = None
    conflicts: tuple[str, ...] = ()


def begin_external_edit(
    kb_dir: Path, identity: DatabaseIdentity, *, view_id="legacy"
) -> ExternalEdit:
    return WikiEditStore(kb_dir, identity, view_id=view_id).begin()


def accept_external_edit(
    kb_dir: Path,
    identity: DatabaseIdentity,
    edit_id: str,
    *,
    editor_stopped: bool,
    view_id="legacy",
) -> EditOutcome:
    store = WikiEditStore(kb_dir, identity, view_id=view_id)
    knowledge = KnowledgeStore(kb_dir, identity)
    with storage_stage(kb_dir, identity), projection_lock(kb_dir):
        draft = store.capture(edit_id, editor_stopped=editor_stopped)
        if draft.conflicts:
            return EditOutcome("conflict", conflicts=draft.conflicts)
        current = knowledge.bind(view_id=view_id)
        master = current.files()
        actual = {path: hashlib.sha256(body).hexdigest() for path, body in draft.files.items()}
        baseline = draft.edit.baseline
        paths = baseline.keys() | master.keys() | actual.keys()
        conflicts = tuple(
            sorted(
                path
                for path in paths
                if actual.get(path) != baseline.get(path)
                and master.get(path) not in {baseline.get(path), actual.get(path)}
            )
        )
        if conflicts:
            return EditOutcome("conflict", current.publication_id, conflicts)
        files = {}
        for path in paths:
            if actual.get(path) == baseline.get(path):
                if path in master:
                    files[path] = current.read(path)
            elif path in draft.files:
                files[path] = draft.files[path]
        prepared = knowledge.prepare_revision(
            files,
            expected=current.publication_id,
            view_id=view_id,
            unit_revisions=current.unit_revisions(),
            index_revisions=current.index_revisions(),
        )
        try:
            result = knowledge.publish(prepared, edit_id=edit_id)
        except (PublicationConflict, ProjectionConflict):
            return EditOutcome("conflict", current.publication_id)
        return EditOutcome("accepted", result.publication_id)


def discard_external_edit(
    kb_dir: Path,
    identity: DatabaseIdentity,
    edit_id: str,
    *,
    editor_stopped: bool,
    discard_unknown: tuple[str, ...] = (),
    view_id="legacy",
) -> EditOutcome:
    store = WikiEditStore(kb_dir, identity, view_id=view_id)
    publication_id, conflicts = store.discard(
        edit_id, editor_stopped=editor_stopped, discard_unknown=discard_unknown
    )
    return EditOutcome("conflict" if conflicts else "discarded", publication_id, conflicts)
