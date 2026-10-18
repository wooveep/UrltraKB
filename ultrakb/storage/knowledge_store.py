"""Immutable revision manifests and short, version-checked publication commits.

This is a development storage boundary. Ready navigation and synchronization
plans bind atomically; complete source/attempt guards still need integration.
"""

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from ultrakb.locks import file_write_lock
from ultrakb.source_records import RecordId
from ultrakb.storage.blobs import Blob, put_blob, read_bytes, retain_blob
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.navigation_store import (
    RecoveryRequired,
    bind_navigation,
    prepare_navigation,
    require_navigation,
    resolve_inputs,
)
from ultrakb.storage.tree_store import TreeStore
from ultrakb.storage.wiki_paths import validate_file_tree
from ultrakb.storage.wiki_paths import wiki_path as _path
from ultrakb.storage.wiki_projection import (
    activate_sync_plan,
    prepare_sync_plan,
    projection_lock,
    require_sync_plan,
)

MAX_BATCH_ROWS = 128
MAX_BATCH_BYTES = 256 * 1024
MAX_ITEM_BYTES = 64 * 1024


class PublicationConflict(ValueError):
    """Recompute or review against the new publication; never overwrite it."""


@dataclass(frozen=True)
class PreparedRevision:
    revision_id: str
    publication_id: str
    view_id: str
    expected_publication: str | None
    manifest_digest: str
    navigation_fingerprint: str
    sync_plan_id: str


@dataclass(frozen=True)
class Publication:
    publication_id: str
    revision_id: str
    view_id: str


def _manifest(entries):
    return json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()


def _batches(entries):
    batch, byte_count = [], 0
    for entry in entries:
        size = len(_manifest(entry)) + 512
        if size > MAX_ITEM_BYTES:
            raise ValueError("Revision entry exceeds the item limit")
        # Every file inserts one manifest row and one reference row.
        cost = 2 * size
        if batch and (2 * len(batch) >= MAX_BATCH_ROWS or byte_count + cost > MAX_BATCH_BYTES):
            yield batch
            batch, byte_count = [], 0
        batch.append(entry)
        byte_count += cost
    if batch:
        yield batch


class BoundKnowledge:
    def __init__(self, root: Path, identity: DatabaseIdentity, publication: Publication):
        self.root, self.identity, self.publication = root, identity, publication

    @property
    def publication_id(self):
        return self.publication.publication_id

    def read(self, path: str) -> bytes:
        _path(path)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT f.digest FROM ukb_publications p JOIN ukb_revision_files f "
                "ON f.revision_id = p.revision_id WHERE p.publication_id = ? "
                "AND p.revision_id = ? AND p.view_id = ? AND f.path = ?",
                (self.publication_id, self.publication.revision_id, self.publication.view_id, path),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(path)
            return read_bytes(db, row[0])

    def files(self) -> dict[str, str]:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return dict(
                db.connection.execute(
                    "SELECT f.path, f.digest FROM ukb_publications p JOIN ukb_revision_files f "
                    "ON f.revision_id = p.revision_id WHERE p.publication_id = ? "
                    "AND p.revision_id = ? AND p.view_id = ? ORDER BY f.path",
                    (self.publication_id, self.publication.revision_id, self.publication.view_id),
                ).fetchall()
            )

    def index_revisions(self) -> tuple[str, ...]:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            rows = db.connection.execute(
                "SELECT i.index_revision_id FROM ukb_publications p JOIN ukb_revision_inputs i "
                "ON i.revision_id = p.revision_id WHERE p.publication_id = ? "
                "AND p.revision_id = ? AND p.view_id = ? AND i.index_revision_id IS NOT NULL "
                "ORDER BY i.index_revision_id",
                (self.publication_id, self.publication.revision_id, self.publication.view_id),
            ).fetchall()
            return tuple(row[0] for row in rows)

    def unit_revisions(self) -> tuple[str, ...]:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return tuple(
                row[0]
                for row in db.connection.execute(
                    "SELECT unit_revision_id FROM ukb_revision_inputs "
                    "WHERE revision_id = ? ORDER BY unit_revision_id",
                    (self.publication.revision_id,),
                ).fetchall()
            )

    @contextmanager
    def navigation(self):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            revision = require_navigation(db, self.publication.revision_id)
            with TreeStore(self.root, self.identity).open(revision.tree_id) as bound:
                yield bound

    def compile_workspace(self, *, required_attachments: tuple[str, ...] = ()):
        from .compile_workspace import materialize_baseline

        return materialize_baseline(self, required_attachments)


class KnowledgeStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def prepare_revision(
        self,
        files: dict[str, bytes],
        *,
        expected: str | None,
        view_id: str = "legacy",
        index_revisions: tuple[str, ...] = (),
        unit_revisions: tuple[str, ...] = (),
    ) -> PreparedRevision:
        if (
            not isinstance(view_id, str)
            or not view_id
            or not isinstance(files, dict)
            or expected is not None
            and (not isinstance(expected, str) or not expected)
        ):
            raise ValueError("A revision requires a view and complete file manifest")
        if len(view_id.encode()) + len((expected or "").encode()) > 63 * 1024:
            raise ValueError("Revision metadata exceeds the encoded storage item limit")
        for path, payload in files.items():
            _path(path)
            if not isinstance(payload, bytes):
                raise ValueError("Revision content must be immutable bytes")
        validate_file_tree(files)
        if not isinstance(index_revisions, tuple) or len(set(index_revisions)) != len(
            index_revisions
        ):
            raise ValueError("A revision requires distinct immutable index identities")
        for index_id in index_revisions:
            TypeAdapter(RecordId).validate_python(index_id)
        revision_id, publication_id, plan_id = (uuid.uuid4().hex for _ in range(3))
        with storage_stage(self.root, self.identity) as db:
            inputs = resolve_inputs(db, unit_revisions, index_revisions)
            entries = []
            for path, payload in sorted(files.items()):
                blob = put_blob(db, payload)
                entries.append([path, blob.digest, blob.byte_count])
            navigation = prepare_navigation(
                self.root, self.identity, revision_id, publication_id, entries, inputs
            )
            manifest = put_blob(
                db,
                _manifest(
                    {
                        "files": entries,
                        "inputs": inputs,
                        "navigation": navigation.input_fingerprint,
                        "sync_plan": plan_id,
                    }
                ),
            )
            prepared = PreparedRevision(
                revision_id,
                publication_id,
                view_id,
                expected,
                manifest.digest,
                navigation.input_fingerprint,
                plan_id,
            )
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO ukb_knowledge_revisions VALUES (?, ?, ?, ?, ?, ?, 'draft')",
                    (
                        revision_id,
                        publication_id,
                        view_id,
                        expected,
                        manifest.digest,
                        navigation.input_fingerprint,
                    ),
                )
                retain_blob(connection, "knowledge-revision", revision_id, "manifest", manifest)
                bind_navigation(connection, revision_id, navigation)
            prepare_sync_plan(db, plan_id, revision_id, view_id, entries)
            for start in range(0, len(inputs), 63):
                input_batch = inputs[start : start + 63]
                with db.transaction() as connection:
                    for unit_id, index_id in input_batch:
                        connection.execute(
                            "INSERT INTO ukb_revision_inputs VALUES (?, ?, ?)",
                            (revision_id, unit_id, index_id),
                        )
            # Each batch is prepared before the short transaction begins.
            for batch in _batches(entries):
                with db.transaction() as connection:
                    state = connection.execute(
                        "SELECT state FROM ukb_knowledge_revisions WHERE revision_id = ?",
                        (revision_id,),
                    ).fetchone()[0]
                    if state != "draft":
                        raise ValueError("A sealed revision cannot be changed")
                    for path, digest, byte_count in batch:
                        connection.execute(
                            "INSERT INTO ukb_revision_files VALUES (?, ?, ?, ?)",
                            (revision_id, path, digest, byte_count),
                        )
                        retain_blob(
                            connection,
                            "knowledge-revision",
                            revision_id,
                            f"file:{path}",
                            Blob(digest, byte_count),
                        )
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE ukb_knowledge_revisions SET state = 'sealed' "
                    "WHERE revision_id = ? AND state = 'draft'",
                    (revision_id,),
                )
            # Verify the sealed object without a SQL write transaction or read snapshot.
            persisted = [
                list(row)
                for row in db.connection.execute(
                    "SELECT path, digest, byte_count FROM ukb_revision_files "
                    "WHERE revision_id = ? ORDER BY path",
                    (revision_id,),
                ).fetchall()
            ]
            retained_inputs = db.connection.execute(
                "SELECT unit_revision_id, index_revision_id FROM ukb_revision_inputs "
                "WHERE revision_id = ? ORDER BY unit_revision_id",
                (revision_id,),
            ).fetchall()
            ready_navigation = require_navigation(db, revision_id)
            require_sync_plan(db, revision_id, plan_id)
            sealed_manifest = _manifest(
                {
                    "files": persisted,
                    "inputs": retained_inputs,
                    "navigation": ready_navigation.input_fingerprint,
                    "sync_plan": plan_id,
                }
            )
            if hashlib.sha256(sealed_manifest).hexdigest() != prepared.manifest_digest:
                raise ValueError("Sealed revision manifest does not match its entries")
            for _, digest, byte_count in persisted:
                if len(read_bytes(db, digest)) != byte_count:
                    raise ValueError("Sealed revision content failed validation")
            with db.transaction() as connection:
                updated = connection.execute(
                    "UPDATE ukb_knowledge_revisions SET state = 'ready' "
                    "WHERE revision_id = ? AND state = 'sealed' AND manifest_digest = ?",
                    (revision_id, prepared.manifest_digest),
                ).rowcount
                if updated != 1:
                    raise ValueError("Revision changed during sealed validation")
            return prepared

    def publish(self, prepared: PreparedRevision, *, edit_id: str | None = None) -> Publication:
        with (
            storage_stage(self.root, self.identity) as db,
            projection_lock(self.root),
            file_write_lock(self.root / ".ultrakb/ingest.lock"),
            db.transaction() as connection,
        ):
            record = connection.execute(
                "SELECT publication_id, view_id, expected_publication, manifest_digest, "
                "navigation_fingerprint, state "
                "FROM ukb_knowledge_revisions WHERE revision_id = ?",
                (prepared.revision_id,),
            ).fetchone()
            if (
                record is None
                or record[:-1]
                != (
                    prepared.publication_id,
                    prepared.view_id,
                    prepared.expected_publication,
                    prepared.manifest_digest,
                    prepared.navigation_fingerprint,
                )
                or record[-1] not in {"ready", "published"}
            ):
                raise ValueError("Only the exact validated revision can be published")
            plan = connection.execute(
                "SELECT plan_id FROM ukb_revision_sync WHERE revision_id = ?",
                (prepared.revision_id,),
            ).fetchone()
            if plan != (prepared.sync_plan_id,):
                raise ValueError("Publication requires its exact synchronization plan")
            require_navigation(db, prepared.revision_id)
            if record[-1] == "published":
                return Publication(prepared.publication_id, prepared.revision_id, prepared.view_id)
            head = connection.execute(
                "SELECT publication_id FROM ukb_heads WHERE view_id = ?", (prepared.view_id,)
            ).fetchone()
            if (head[0] if head else None) != prepared.expected_publication:
                raise PublicationConflict("Knowledge changed; recompute or review the candidate")
            activate_sync_plan(db, prepared.revision_id, prepared.sync_plan_id)
            connection.execute(
                "INSERT INTO ukb_publications VALUES (?, ?, ?, ?)",
                (
                    prepared.publication_id,
                    prepared.revision_id,
                    prepared.view_id,
                    self.identity.generation,
                ),
            )
            connection.execute(
                "INSERT INTO ukb_heads VALUES (?, ?) ON CONFLICT(view_id) "
                "DO UPDATE SET publication_id = excluded.publication_id",
                (prepared.view_id, prepared.publication_id),
            )
            connection.execute(
                "UPDATE ukb_knowledge_revisions SET state = 'published' WHERE revision_id = ?",
                (prepared.revision_id,),
            )
            if edit_id is not None:
                from .wiki_edit_store import finish_edit

                finish_edit(
                    connection, prepared.view_id, edit_id, prepared.publication_id, "accepted"
                )
        return Publication(prepared.publication_id, prepared.revision_id, prepared.view_id)

    def bind(self, publication_id: str | None = None, *, view_id: str = "legacy") -> BoundKnowledge:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if publication_id is None:
                head = db.connection.execute(
                    "SELECT publication_id FROM ukb_heads WHERE view_id = ?", (view_id,)
                ).fetchone()
                if not head:
                    raise FileNotFoundError("No complete public knowledge is available")
                publication_id = head[0]
            row = db.connection.execute(
                "SELECT revision_id FROM ukb_publications WHERE publication_id = ? AND view_id = ?",
                (publication_id, view_id),
            ).fetchone()
            if not row:
                raise FileNotFoundError("This knowledge publication is unavailable")
            require_navigation(db, row[0])
            return BoundKnowledge(
                self.root, self.identity, Publication(publication_id, row[0], view_id)
            )

    def repair_navigation(self, publication_id: str, *, view_id: str = "legacy"):
        """Rebuild the same deterministic input only through this explicit use case."""
        with storage_stage(self.root, self.identity) as db:
            record = db.connection.execute(
                "SELECT r.revision_id, r.navigation_fingerprint FROM ukb_publications p "
                "JOIN ukb_knowledge_revisions r ON r.revision_id = p.revision_id "
                "WHERE p.publication_id = ? AND p.view_id = ?",
                (publication_id, view_id),
            ).fetchone()
            if not record:
                raise FileNotFoundError("This knowledge publication is unavailable")
            try:
                return require_navigation(db, record[0])
            except RecoveryRequired:
                pass
            files = db.connection.execute(
                "SELECT path, digest, byte_count FROM ukb_revision_files "
                "WHERE revision_id = ? ORDER BY path",
                (record[0],),
            ).fetchall()
            inputs = db.connection.execute(
                "SELECT unit_revision_id, index_revision_id FROM ukb_revision_inputs "
                "WHERE revision_id = ? ORDER BY unit_revision_id",
                (record[0],),
            ).fetchall()
            tree = prepare_navigation(
                self.root, self.identity, record[0], publication_id, files, inputs
            )
            if tree.input_fingerprint != record[1]:
                raise RecoveryRequired("Knowledge navigation policy or committed inputs changed")
            with db.transaction() as connection:
                bind_navigation(connection, record[0], tree)
            return tree
