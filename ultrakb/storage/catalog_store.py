"""Sources retain their own identity while input bytes share CAS storage."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ultrakb.storage.blobs import put_blob, read_bytes, retain_blob
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import DatabaseIdentity


@dataclass(frozen=True)
class InputFile:
    path: str
    digest: str
    byte_count: int
    kind: str

    def __post_init__(self):
        if (
            not isinstance(self.path, str)
            or not self.path
            or PurePosixPath(self.path).is_absolute()
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or "\\" in self.path
            or not isinstance(self.digest, str)
            or len(self.digest) != 64
            or any(value not in "0123456789abcdef" for value in self.digest)
            or type(self.byte_count) is not int
            or self.byte_count < 0
            or not isinstance(self.kind, str)
            or self.kind not in {"main", "sidecar"}
        ):
            raise ValueError("Invalid retained input manifest entry")


@dataclass(frozen=True)
class SourceRevision:
    source_id: str
    revision_id: str
    ordinal: int
    files: tuple[InputFile, ...]


class CatalogStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def admit(self, path: Path, *, source_key: str) -> SourceRevision:
        if not source_key or not isinstance(source_key, str):
            raise ValueError("Source identity must be explicit")
        payload = path.read_bytes()
        with storage_stage(self.root, self.identity) as db:
            content = put_blob(db, payload)
            original = InputFile(path.name, content.digest, content.byte_count, "main")
            manifest = put_blob(
                db, json.dumps([original.__dict__], ensure_ascii=False, sort_keys=True).encode()
            )
            source = db.connection.execute(
                "SELECT source_id FROM ukb_sources WHERE source_key = ?", (source_key,)
            ).fetchone()
            source_id = source[0] if source else uuid.uuid4().hex
            revision_id = uuid.uuid4().hex
            # Source ordering and its Blob references commit together.
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO ukb_sources VALUES (?, ?, ?) ON CONFLICT(source_key) DO NOTHING",
                    (source_id, source_key, path.name),
                )
                source_id = connection.execute(
                    "SELECT source_id FROM ukb_sources WHERE source_key = ?", (source_key,)
                ).fetchone()[0]
                ordinal = connection.execute(
                    "SELECT coalesce(max(ordinal), -1) + 1 FROM ukb_source_revisions "
                    "WHERE source_id = ?",
                    (source_id,),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO ukb_source_revisions VALUES (?, ?, ?, ?)",
                    (revision_id, source_id, ordinal, manifest.digest),
                )
                retain_blob(connection, "source-revision", revision_id, "manifest", manifest)
                retain_blob(
                    connection, "source-revision", revision_id, f"file:{original.path}", content
                )
            return SourceRevision(source_id, revision_id, ordinal, (original,))

    def revision(self, revision_id: str) -> SourceRevision:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._revision(db, revision_id)

    @staticmethod
    def _revision(db, revision_id):
        row = db.connection.execute(
            "SELECT source_id, ordinal, manifest_digest FROM ukb_source_revisions "
            "WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise KeyError(revision_id)
        values = json.loads(read_bytes(db, row[2]))
        if not isinstance(values, list) or not values:
            raise ValueError("Invalid retained input manifest")
        files = tuple(InputFile(**value) for value in values)
        if sum(file.kind == "main" for file in files) != 1:
            raise ValueError("Input manifest must contain one main file")
        return SourceRevision(row[0], revision_id, row[1], files)

    def read_original(self, revision_id: str) -> bytes:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            revision = self._revision(db, revision_id)
            original = next(file for file in revision.files if file.kind == "main")
            return read_bytes(db, original.digest)
