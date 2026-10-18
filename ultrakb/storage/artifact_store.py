"""Accept immutable generated files before independently synchronizing their copies."""

import hashlib
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from pydantic import TypeAdapter

from ultrakb.file_state import file_versions
from ultrakb.locks import (
    LockCancelled,
    atomic_write_bytes,
    file_write_lock,
    sync_file_and_parents,
)
from ultrakb.mutation import RecoveryRequired
from ultrakb.source_records import RecordId

from .blobs import put_blob, read_bytes, retain_blob
from .coordination import storage_stage
from .execution_store import now, require_active_execution
from .wiki_paths import page_after, wiki_path


def artifact_path(value):
    wiki_path(value)
    if not value.startswith("output/"):
        raise ValueError("Artifacts require a logical file path under output/")
    return value


def _epoch(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("Artifact epochs require a nonnegative bounded integer")


def _request(path, payload, expected_epoch, execution_id, evidence_publication_id):
    artifact_path(path)
    _epoch(expected_epoch)
    if not isinstance(payload, bytes) or len(path.encode()) > 63 * 1024:
        raise ValueError("Artifacts require immutable bytes and a bounded logical name")
    for identity in (execution_id, evidence_publication_id):
        TypeAdapter(RecordId).validate_python(identity)
    return (
        path,
        hashlib.sha256(payload).hexdigest(),
        expected_epoch,
        execution_id,
        evidence_publication_id,
    )


@dataclass(frozen=True)
class ArtifactRevision:
    status: str
    revision_id: str
    path: str
    digest: str
    epoch: int | None


@dataclass(frozen=True)
class ArtifactProjection:
    status: str
    revision_id: str | None
    error_code: str | None = None


@dataclass(frozen=True)
class ArtifactEntry:
    path: str
    revision_id: str
    digest: str
    byte_count: int


@dataclass(frozen=True)
class ArtifactFiles:
    files: tuple[ArtifactEntry, ...]
    next_after: str | None
    epoch: int


class ArtifactStore:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def epoch(self):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._current(db)

    @staticmethod
    def _current(db):
        row = db.connection.execute(
            "SELECT epoch FROM ukb_artifact_state WHERE slot = 1"
        ).fetchone()
        if row is None:
            raise RecoveryRequired("Artifact state is unavailable")
        return row[0]

    @staticmethod
    def _path_available(db, path):
        # The ingest lease excludes other head writers. These indexed reads run
        # before BEGIN, without scanning unrelated names or holding a SQL writer.
        parent = path.rpartition("/")[0]
        while parent:
            if db.connection.execute(
                "SELECT 1 FROM ukb_artifact_heads WHERE path = ?", (parent,)
            ).fetchone():
                return False
            parent = parent.rpartition("/")[0]
        return (
            db.connection.execute(
                "SELECT 1 FROM ukb_artifact_heads WHERE path >= ? AND path < ? LIMIT 1",
                (path + "/", path + "0"),
            ).fetchone()
            is None
        )

    def read(self, path, *, epoch=None, revision_id=None):
        artifact_path(path)
        if epoch is not None and revision_id is not None:
            raise ValueError("Select an artifact epoch or an exact revision")
        if revision_id is not None:
            TypeAdapter(RecordId).validate_python(revision_id)
        if epoch is not None:
            _epoch(epoch)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            selected = self._entry(db, path, epoch=epoch, revision_id=revision_id)
            return read_bytes(db, selected.digest)

    def entry(self, path, *, epoch=None):
        artifact_path(path)
        if epoch is not None:
            _epoch(epoch)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._entry(db, path, epoch=epoch)

    def _entry(self, db, path, *, epoch, revision_id=None):
        if revision_id is not None:
            row = db.connection.execute(
                "SELECT path, revision_id, digest, byte_count FROM ukb_artifact_revisions "
                "WHERE revision_id = ? AND path = ?",
                (revision_id, path),
            ).fetchone()
        else:
            current = self._current(db)
            if epoch is not None and epoch > current:
                raise ValueError("Artifact epoch has not been accepted")
            row = db.connection.execute(
                "SELECT path, revision_id, digest, byte_count FROM ukb_artifact_revisions "
                "WHERE path = ? AND state = 'accepted' AND epoch <= ? "
                "ORDER BY epoch DESC LIMIT 1",
                (path, current if epoch is None else epoch),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(path)
        return ArtifactEntry(*row)

    def files(self, *, prefix="output/", epoch=None, after=None, limit=63):
        if prefix != "output/":
            if not prefix.endswith("/"):
                raise ValueError("Select an artifact directory prefix")
            artifact_path(prefix[:-1])
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Artifact page size must be between 1 and 128")
        lower, exclusive = page_after(prefix, after)
        if epoch is not None:
            _epoch(epoch)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            current = self._current(db)
            selected = current if epoch is None else epoch
            if selected > current:
                raise ValueError("Artifact epoch has not been accepted")
            # Limit candidate names first. Newly accepted names may have no
            # version in this snapshot; they still advance the bounded cursor.
            rows = db.connection.execute(
                "SELECT h.path, r.revision_id, r.digest, r.byte_count "
                "FROM ukb_artifact_heads h LEFT JOIN ukb_artifact_revisions r "
                "ON r.revision_id = (SELECT revision_id FROM ukb_artifact_revisions v "
                "WHERE v.path = h.path AND v.state = 'accepted' AND v.epoch <= ? "
                "ORDER BY v.epoch DESC LIMIT 1) "
                "WHERE h.path >= ? AND h.path < ? AND h.path > ? "
                "ORDER BY h.path LIMIT ?",
                (selected, lower, prefix[:-1] + "0", exclusive, limit + 1),
            ).fetchall()
            return ArtifactFiles(
                tuple(ArtifactEntry(*row) for row in rows[:limit] if row[1] is not None),
                rows[limit - 1][0] if len(rows) > limit else None,
                selected,
            )

    @staticmethod
    def _committed(db, revision_id, fingerprint):
        row = db.connection.execute(
            "SELECT state, revision_id, path, digest, epoch, expected_epoch, execution_id, "
            "evidence_publication_id FROM ukb_artifact_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        if (row[2], row[3], *row[5:]) != fingerprint:
            raise ValueError("Artifact operation identity does not match its original request")
        return ArtifactRevision("saved" if row[0] == "accepted" else "conflict", *row[1:5])

    def committed_write(
        self, revision_id, *, path, payload, expected_epoch, execution_id, evidence_publication_id
    ):
        TypeAdapter(RecordId).validate_python(revision_id)
        fingerprint = _request(path, payload, expected_epoch, execution_id, evidence_publication_id)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._committed(db, revision_id, fingerprint)

    def save(
        self,
        path,
        payload,
        *,
        expected_epoch,
        execution_id,
        evidence_publication_id,
        check_cancelled=lambda: None,
        revision_id=None,
    ):
        fingerprint = _request(path, payload, expected_epoch, execution_id, evidence_publication_id)
        if expected_epoch == 2**63 - 1:
            raise ValueError("Artifact epoch cannot advance past the storage range")
        revision_id = uuid4().hex if revision_id is None else revision_id
        timestamp = now()
        TypeAdapter(RecordId).validate_python(revision_id)

        def cancelled():
            check_cancelled()
            return False

        with storage_stage(self.root, self.identity) as db:
            committed = self._committed(db, revision_id, fingerprint)
            if committed is not None:
                return committed
            require_active_execution(db.connection, self.identity, execution_id)
            content = put_blob(db, payload)
            with file_write_lock(self.root / ".ultrakb/ingest.lock", cancelled=cancelled):
                available = self._path_available(db, path)
                with db.transaction() as connection:
                    return self._accept(
                        db,
                        connection,
                        revision_id,
                        fingerprint,
                        content,
                        timestamp,
                        available=available,
                        check_cancelled=check_cancelled,
                    )

    def _accept(
        self,
        db,
        connection,
        revision_id,
        fingerprint,
        content,
        timestamp,
        *,
        available,
        check_cancelled,
    ):
        path, _, expected_epoch, execution_id, evidence_publication_id = fingerprint
        committed = self._committed(db, revision_id, fingerprint)
        if committed is not None:
            return committed
        require_active_execution(connection, self.identity, execution_id)
        check_cancelled()
        current = self._current(db)
        accepted = current == expected_epoch and available
        epoch = current + 1 if accepted else None
        connection.execute(
            "INSERT INTO ukb_artifact_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                path,
                content.digest,
                content.byte_count,
                execution_id,
                evidence_publication_id,
                expected_epoch,
                "accepted" if accepted else "conflict",
                epoch,
                timestamp,
            ),
        )
        retain_blob(connection, "artifact-revision", revision_id, "content", content)
        if accepted:
            connection.execute("UPDATE ukb_artifact_state SET epoch = ? WHERE slot = 1", (epoch,))
            connection.execute(
                "INSERT INTO ukb_artifact_heads(path, revision_id, applied_digest) "
                "VALUES (?, ?, NULL) ON CONFLICT(path) "
                "DO UPDATE SET revision_id = excluded.revision_id",
                (path, revision_id),
            )
        return ArtifactRevision(
            "saved" if accepted else "conflict", revision_id, path, content.digest, epoch
        )

    def sync(self, path, *, revision_id=None, check_cancelled=lambda: None):
        artifact_path(path)
        if revision_id is not None:
            TypeAdapter(RecordId).validate_python(revision_id)

        def cancelled():
            check_cancelled()
            return False

        try:
            with (
                storage_stage(self.root, self.identity) as db,
                file_write_lock(self.root / ".ultrakb/ingest.lock", cancelled=cancelled),
            ):
                row = db.connection.execute(
                    "SELECT r.revision_id, r.digest, h.applied_digest, h.pending_revision_id, "
                    "h.pending_base_digest, pending.digest FROM ukb_artifact_heads h "
                    "JOIN ukb_artifact_revisions r ON r.revision_id = h.revision_id "
                    "LEFT JOIN ukb_artifact_revisions pending "
                    "ON pending.revision_id = h.pending_revision_id AND pending.path = h.path "
                    "WHERE h.path = ?",
                    (path,),
                ).fetchone()
                if row is None:
                    raise FileNotFoundError(path)
                if revision_id is not None and revision_id != row[0]:
                    return ArtifactProjection("superseded", revision_id)
                revision_id = row[0]
                target = self.root / path
                if any(
                    part.is_symlink() for part in (target, *target.parents) if part != self.root
                ):
                    return ArtifactProjection("changed", revision_id, "artifact_symlink")
                if target.is_dir():
                    return ArtifactProjection("changed", revision_id, "artifact_is_directory")
                actual = file_versions(self.root, [target]).get(path)
                allowed = {row[1], row[2]}
                if row[3] is not None:
                    if row[5] is None:
                        raise RecoveryRequired("Artifact projection activation is invalid")
                    allowed.update((row[4], row[5]))
                if actual not in allowed:
                    return ArtifactProjection("changed", revision_id, "external_artifact_changed")
                check_cancelled()
                # Retain both the validated physical baseline and the target that
                # may replace it. A newer master head preserves these until a
                # durable copy receipt, including a crash before replacement.
                with db.transaction() as connection:
                    if (
                        connection.execute(
                            "UPDATE ukb_artifact_heads SET pending_revision_id = ?, "
                            "pending_base_digest = ? WHERE path = ? AND revision_id = ?",
                            (revision_id, actual, path, revision_id),
                        ).rowcount
                        != 1
                    ):
                        raise RecoveryRequired("Artifact target changed before activation")
                if actual != row[1]:
                    atomic_write_bytes(target, read_bytes(db, row[1]))
                sync_file_and_parents(target, ancestor=self.root)
                with db.transaction() as connection:
                    if (
                        connection.execute(
                            "UPDATE ukb_artifact_heads SET applied_digest = ?, "
                            "pending_revision_id = NULL, pending_base_digest = NULL "
                            "WHERE path = ? AND revision_id = ? AND pending_revision_id = ?",
                            (row[1], path, revision_id, revision_id),
                        ).rowcount
                        != 1
                    ):
                        raise RecoveryRequired("Artifact activation changed before confirmation")
                return ArtifactProjection("applied", revision_id)
        except (OSError, sqlite3.Error, LockCancelled) as error:
            return ArtifactProjection("pending", revision_id, type(error).__name__)
