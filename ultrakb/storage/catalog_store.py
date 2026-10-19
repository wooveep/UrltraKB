"""Sources retain their own identity while input bytes share CAS storage."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import TypeAdapter

from ultrakb.processing_policy import ProcessingDecision
from ultrakb.source_records import Digest, DocName, RecordId
from ultrakb.storage.blobs import Blob, put_blob, read_bytes, retain_blob
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
            or ":" in self.path
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


@dataclass(frozen=True)
class SourceTarget:
    source_id: str
    source_key: str
    label: str
    revision_id: str
    generation: int = 0
    removed: bool = False
    contribution_empty: bool = False


@dataclass(frozen=True)
class ProcessingVersion:
    processing_id: str
    source_revision_id: str
    fingerprint: str
    body_path: str
    files: tuple[InputFile, ...]
    decision: ProcessingDecision
    source_path: str
    source_kind: str
    content_state: str = "content"
    reprocessing_request_id: str | None = None


@dataclass(frozen=True)
class UnitTarget:
    unit_id: str
    unit_revision_id: str
    source_id: str
    processing_id: str
    source_revision_id: str
    key: str
    doc_name: str
    generation: int


class CatalogConflict(ValueError):
    """A captured source target changed before admission; re-read before retrying."""


def file_batches(files):
    """Prepare bounded reference rows before entering their write transaction."""
    batch, size = [], 0
    for file in files:
        encoded_size = len(json.dumps(file.__dict__, ensure_ascii=False).encode())
        if encoded_size > 64 * 1024:
            raise ValueError("Input manifest entry exceeds the storage item limit")
        if batch and (len(batch) >= 63 or size + 2 * encoded_size > 256 * 1024):
            yield batch
            batch, size = [], 0
        batch.append(file)
        size += 2 * encoded_size
    if batch:
        yield batch


class CatalogStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def admit(
        self,
        path: Path,
        *,
        source_key: str,
        sidecars: dict[str, Path] | None = None,
        expected: str | None = None,
    ) -> SourceRevision:
        if not source_key or not isinstance(source_key, str):
            raise ValueError("Source identity must be explicit")
        if len(source_key.encode()) + len(path.name.encode()) > 64 * 1024:
            raise ValueError("Source metadata exceeds the storage item limit")
        inputs = {path.name: (path, "main")}
        for logical_path, source in (sidecars or {}).items():
            InputFile(logical_path, "0" * 64, 0, "sidecar")
            if logical_path in inputs:
                raise ValueError("Input paths must be unique")
            inputs[logical_path] = (source, "sidecar")
        with storage_stage(self.root, self.identity) as db:
            source_row = db.connection.execute(
                "SELECT source_id, generation, state FROM ukb_sources WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            source_id = source_row[0] if source_row else uuid.uuid4().hex
            source_generation = source_row[1] if source_row else 0
            if source_row and source_row[2] != "active":
                raise CatalogConflict("The source was withdrawn; its history remains available")
            previous = self._latest(db, source_id)
            captured = previous[0] if previous else None
            if expected is not None and captured != expected:
                raise CatalogConflict("The source no longer matches the expected revision")
            files = []
            for logical_path, (source_path, kind) in sorted(inputs.items()):
                content = put_blob(db, source_path.read_bytes())
                files.append(InputFile(logical_path, content.digest, content.byte_count, kind))
            # Encoding and all payload I/O precede source-reference transactions.
            manifest = put_blob(
                db,
                json.dumps(
                    [file.__dict__ for file in files], ensure_ascii=False, sort_keys=True
                ).encode(),
            )
            if previous and previous[1] == manifest.digest:
                return self._revision(db, previous[0])
            revision_id = uuid.uuid4().hex
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO ukb_sources (source_id, source_key, label) VALUES (?, ?, ?) "
                    "ON CONFLICT(source_key) DO NOTHING",
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
                    "INSERT INTO ukb_source_revisions VALUES (?, ?, ?, ?, 'draft')",
                    (revision_id, source_id, ordinal, manifest.digest),
                )
                retain_blob(connection, "source-revision", revision_id, "manifest", manifest)
            for batch in file_batches(files):
                with db.transaction() as connection:
                    for file in batch:
                        retain_blob(
                            connection,
                            "source-revision",
                            revision_id,
                            f"file:{file.path}",
                            Blob(file.digest, file.byte_count),
                        )
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE ukb_source_revisions SET state = 'sealed' WHERE revision_id = ? "
                    "AND state = 'draft'",
                    (revision_id,),
                )
            references = dict(
                db.connection.execute(
                    "SELECT slot, digest FROM ukb_blob_refs WHERE owner_kind = 'source-revision' "
                    "AND owner_id = ?",
                    (revision_id,),
                ).fetchall()
            )
            if references != {
                "manifest": manifest.digest,
                **{f"file:{file.path}": file.digest for file in files},
            }:
                raise ValueError("Sealed input references do not match their manifest")
            with db.transaction() as connection:
                latest = self._latest(db, source_id)
                if (latest[0] if latest else None) != captured:
                    raise CatalogConflict("The source changed while its input was being retained")
                changed = connection.execute(
                    "UPDATE ukb_sources SET generation = generation + 1, contribution_empty = 0 "
                    "WHERE source_id = ? AND state = 'active' AND generation = ?",
                    (source_id, source_generation),
                ).rowcount
                if changed != 1:
                    raise CatalogConflict("The source changed while its input was being retained")
                changed = connection.execute(
                    "UPDATE ukb_source_revisions SET state = 'ready' WHERE revision_id = ? "
                    "AND state = 'sealed'",
                    (revision_id,),
                ).rowcount
                if changed != 1:
                    raise ValueError("The sealed input changed during admission")
            return SourceRevision(source_id, revision_id, ordinal, tuple(files))

    @staticmethod
    def _latest(db, source_id):
        return db.connection.execute(
            "SELECT revision_id, manifest_digest FROM ukb_source_revisions "
            "WHERE source_id = ? AND state = 'ready' ORDER BY ordinal DESC LIMIT 1",
            (source_id,),
        ).fetchone()

    def revision(self, revision_id: str) -> SourceRevision:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._revision(db, revision_id)

    def source(self, source_id: str) -> SourceTarget:
        TypeAdapter(RecordId).validate_python(source_id)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT source_key, label, generation, state, contribution_empty "
                "FROM ukb_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            revision = self._latest(db, source_id) if row else None
            if row is None or revision is None:
                raise KeyError(source_id)
            return SourceTarget(
                source_id, row[0], row[1], revision[0], row[2], row[3] == "withdrawn", bool(row[4])
            )

    def source_page(
        self, *, after: str = "", limit: int = 63, include_removed: bool = False
    ) -> tuple[SourceTarget, ...]:
        """List admitted source identities without opening originals or processing bodies."""
        if (
            not isinstance(after, str)
            or type(limit) is not int
            or not 1 <= limit <= 128
            or type(include_removed) is not bool
        ):
            raise ValueError("Source pages require a string cursor and a limit of 1..128")
        with storage_stage(self.root, self.identity, read_only=True) as db:
            rows = db.connection.execute(
                "SELECT s.source_id, s.source_key, s.label, "
                "(SELECT r.revision_id FROM ukb_source_revisions r "
                "WHERE r.source_id = s.source_id AND r.state = 'ready' "
                "ORDER BY r.ordinal DESC LIMIT 1) AS target, s.generation, s.state = 'withdrawn', "
                "s.contribution_empty "
                "FROM ukb_sources s WHERE s.source_id > ? AND target IS NOT NULL "
                "AND (? OR s.state = 'active') "
                "ORDER BY s.source_id LIMIT ?",
                (after, include_removed, limit),
            ).fetchall()
            return tuple(
                SourceTarget(row[0], row[1], row[2], row[3], row[4], bool(row[5]), bool(row[6]))
                for row in rows
            )

    @staticmethod
    def _revision(db, revision_id):
        row = db.connection.execute(
            "SELECT source_id, ordinal, manifest_digest FROM ukb_source_revisions "
            "WHERE revision_id = ? AND state = 'ready'",
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

    def read_file(self, revision_id: str, path: str) -> bytes:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            revision = self._revision(db, revision_id)
            file = next((file for file in revision.files if file.path == path), None)
            if file is None:
                raise FileNotFoundError(path)
            return read_bytes(db, file.digest)

    def retain_processing(
        self,
        revision_id: str,
        files: dict[str, Path],
        *,
        body_path: str,
        source_path: str | None = None,
        source_kind: str = "markdown",
        fingerprint: str,
        operation_id: str,
        decision: ProcessingDecision,
        content_state: str = "content",
        reprocessing_request_id: str | None = None,
    ) -> ProcessingVersion:
        if reprocessing_request_id is not None:
            TypeAdapter(Digest).validate_python(reprocessing_request_id)
        if (
            body_path not in files
            or not isinstance(fingerprint, str)
            or not fingerprint
            or not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(decision, ProcessingDecision)
            or content_state
            not in {"content", "empty", "withdrawn", "objects_only", "parse_failed", "unreadable"}
        ):
            raise ValueError("Processing requires a complete body, policy and operation identity")
        for path in files:
            InputFile(path, "0" * 64, 0, "main" if path == body_path else "sidecar")
        source_path = source_path if source_path is not None else body_path
        if (
            not isinstance(source_path, str)
            or source_path not in files
            or not isinstance(source_kind, str)
            or source_kind not in {"markdown", "text", "pages"}
        ):
            raise ValueError("Processing requires its retained normalized source and protocol")
        decision_json = decision.model_dump_json()
        if (
            len(
                json.dumps(
                    (
                        revision_id,
                        fingerprint,
                        operation_id,
                        body_path,
                        decision_json,
                        source_path,
                        source_kind,
                        content_state,
                    ),
                    ensure_ascii=False,
                ).encode()
            )
            > 64 * 1024
        ):
            raise ValueError("Processing metadata exceeds the storage item limit")
        with storage_stage(self.root, self.identity) as db:
            self._revision(db, revision_id)
            if reprocessing_request_id is not None:
                request = db.connection.execute(
                    "SELECT source_revision_id FROM ukb_reprocessing_requests WHERE request_id = ?",
                    (reprocessing_request_id,),
                ).fetchone()
                if request != (revision_id,):
                    raise CatalogConflict("Processing requires its exact reprocessing request")
            captured = []
            for path, source in sorted(files.items()):
                content = put_blob(db, source.read_bytes())
                captured.append(
                    InputFile(
                        path,
                        content.digest,
                        content.byte_count,
                        "main" if path == body_path else "sidecar",
                    )
                )
            manifest = put_blob(
                db,
                json.dumps(
                    [file.__dict__ for file in captured], ensure_ascii=False, sort_keys=True
                ).encode(),
            )
            expected = (
                revision_id,
                fingerprint,
                body_path,
                manifest.digest,
                decision_json,
                source_path,
                source_kind,
                content_state,
                reprocessing_request_id,
            )
            with db.transaction() as connection:
                existing = connection.execute(
                    "SELECT processing_id, source_revision_id, fingerprint, body_path, "
                    "manifest_digest, decision_json, source_path, source_kind, content_state, "
                    "reprocessing_request_id, "
                    "state "
                    "FROM ukb_processing_versions "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if existing and existing[1:10] != expected:
                    raise CatalogConflict(
                        "The processing operation already accepted different inputs"
                    )
                processing_id = existing[0] if existing else uuid.uuid4().hex
                if not existing:
                    connection.execute(
                        "INSERT INTO ukb_processing_versions "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')",
                        (
                            processing_id,
                            revision_id,
                            fingerprint,
                            operation_id,
                            body_path,
                            manifest.digest,
                            decision_json,
                            source_path,
                            source_kind,
                            content_state,
                            reprocessing_request_id,
                        ),
                    )
                    retain_blob(
                        connection, "processing-version", processing_id, "manifest", manifest
                    )
            if existing and existing[10] == "ready":
                return self._processing(db, processing_id)
            if not existing or existing[10] == "draft":
                for batch in file_batches(captured):
                    with db.transaction() as connection:
                        state = connection.execute(
                            "SELECT state FROM ukb_processing_versions WHERE processing_id = ?",
                            (processing_id,),
                        ).fetchone()[0]
                        if state != "draft":
                            break
                        for file in batch:
                            retain_blob(
                                connection,
                                "processing-version",
                                processing_id,
                                f"file:{file.path}",
                                Blob(file.digest, file.byte_count),
                            )
                with db.transaction() as connection:
                    connection.execute(
                        "UPDATE ukb_processing_versions SET state = 'sealed' "
                        "WHERE processing_id = ? AND state = 'draft'",
                        (processing_id,),
                    )
            references = dict(
                db.connection.execute(
                    "SELECT slot, digest FROM ukb_blob_refs "
                    "WHERE owner_kind = 'processing-version' "
                    "AND owner_id = ?",
                    (processing_id,),
                ).fetchall()
            )
            if references != {
                "manifest": manifest.digest,
                **{f"file:{file.path}": file.digest for file in captured},
            }:
                raise ValueError("Sealed processing references do not match the accepted manifest")
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE ukb_processing_versions SET state = 'ready' "
                    "WHERE processing_id = ? AND state = 'sealed'",
                    (processing_id,),
                )
            return self._processing(db, processing_id)

    @staticmethod
    def _processing(db, processing_id):
        row = db.connection.execute(
            "SELECT source_revision_id, fingerprint, body_path, manifest_digest, decision_json, "
            "source_path, source_kind, content_state, reprocessing_request_id "
            "FROM ukb_processing_versions WHERE processing_id = ? AND state = 'ready'",
            (processing_id,),
        ).fetchone()
        if row is None:
            raise KeyError(processing_id)
        values = json.loads(read_bytes(db, row[3]))
        if not isinstance(values, list) or not values:
            raise ValueError("Invalid processing manifest")
        files = tuple(InputFile(**value) for value in values)
        if sum(file.kind == "main" and file.path == row[2] for file in files) != 1:
            raise ValueError("Processing manifest is missing its exact main body")
        if row[5] not in {file.path for file in files}:
            raise ValueError("Processing manifest is missing its exact normalized source")
        return ProcessingVersion(
            processing_id,
            row[0],
            row[1],
            row[2],
            files,
            ProcessingDecision.model_validate_json(row[4]),
            row[5],
            row[6],
            row[7],
            row[8],
        )

    def processing(self, processing_id: str) -> ProcessingVersion:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._processing(db, processing_id)

    def read_processed(self, processing_id: str, path: str) -> bytes:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            version = self._processing(db, processing_id)
            file = next((file for file in version.files if file.path == path), None)
            if file is None:
                raise FileNotFoundError(path)
            return read_bytes(db, file.digest)

    def plan_unit(
        self,
        processing_id: str,
        *,
        key: str,
        doc_name: str,
        expected_generation: int | None = None,
        expected_source_generation: int | None = None,
    ) -> UnitTarget:
        TypeAdapter(DocName).validate_python(doc_name)
        if (
            not isinstance(key, str)
            or not key
            or len(key.encode()) + len(doc_name.encode()) > 65536
        ):
            raise ValueError("A processing unit requires a bounded stable key and document name")
        if expected_generation is not None and (
            type(expected_generation) is not int or expected_generation < 0
        ):
            raise ValueError(
                "A processing-unit generation must be nonnegative; zero requires absence"
            )
        if expected_source_generation is not None and (
            type(expected_source_generation) is not int
            or not 0 <= expected_source_generation < 2**63
        ):
            raise ValueError("A source generation must be a nonnegative storage integer")
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            processing = connection.execute(
                "SELECT p.source_revision_id, s.source_id, p.reprocessing_request_id "
                "FROM ukb_processing_versions p "
                "JOIN ukb_source_revisions s ON s.revision_id = p.source_revision_id "
                "WHERE p.processing_id = ? AND p.state = 'ready' AND s.state = 'ready'",
                (processing_id,),
            ).fetchone()
            if not processing:
                raise KeyError(processing_id)
            from .reprocessing_store import require_current_reprocessing

            require_current_reprocessing(connection, processing[0], processing[2])
            latest = self._latest(db, processing[1])
            if not latest or latest[0] != processing[0]:
                raise CatalogConflict("The source target changed before unit preparation")
            source = connection.execute(
                "SELECT state, generation, contribution_empty FROM ukb_sources WHERE source_id = ?",
                (processing[1],),
            ).fetchone()
            if source is None or source[0] != "active":
                raise CatalogConflict("The source was withdrawn before unit preparation")
            if (
                expected_source_generation is not None and source[1] != expected_source_generation
            ) or (source[2] and processing[2] is None):
                raise CatalogConflict("The source contribution changed before unit preparation")
            unit = connection.execute(
                "SELECT unit_id, target_revision_id, generation, doc_name FROM ukb_import_units "
                "WHERE source_id = ? AND unit_key = ?",
                (processing[1], key),
            ).fetchone()
            if expected_generation is not None and (
                (expected_generation == 0 and unit is not None)
                or (expected_generation > 0 and (not unit or unit[2] != expected_generation))
            ):
                raise CatalogConflict("The processing unit changed before the expected update")
            if unit:
                if unit[3] != doc_name:
                    raise CatalogConflict("A unit document name cannot be silently reassigned")
                current = self._unit_revision(db, unit[1])
                if current.processing_id == processing_id:
                    return current
            unit_id = unit[0] if unit else uuid.uuid4().hex
            if not unit:
                connection.execute(
                    "INSERT INTO ukb_import_units VALUES (?, ?, ?, ?, NULL, 0)",
                    (unit_id, processing[1], key, doc_name),
                )
            revision_id = uuid.uuid4().hex
            generation = unit[2] + 1 if unit else 1
            connection.execute(
                "INSERT INTO ukb_unit_revisions VALUES (?, ?, ?, ?)",
                (revision_id, unit_id, processing_id, generation),
            )
            connection.execute(
                "UPDATE ukb_import_units SET target_revision_id = ?, generation = ? "
                "WHERE unit_id = ?",
                (revision_id, generation, unit_id),
            )
            connection.execute(
                "UPDATE ukb_sources SET generation = generation + 1, contribution_empty = 0 "
                "WHERE source_id = ?",
                (processing[1],),
            )
            if processing[2] is not None:
                connection.execute(
                    "UPDATE ukb_reprocessing_requests "
                    "SET expected_source_generation = expected_source_generation + 1 "
                    "WHERE request_id = ?",
                    (processing[2],),
                )
            return UnitTarget(
                unit_id,
                revision_id,
                processing[1],
                processing_id,
                processing[0],
                key,
                doc_name,
                generation,
            )

    @staticmethod
    def _unit_revision(db, revision_id):
        row = db.connection.execute(
            "SELECT u.unit_id, r.unit_revision_id, u.source_id, r.processing_id, "
            "p.source_revision_id, u.unit_key, u.doc_name, r.generation "
            "FROM ukb_unit_revisions r JOIN ukb_import_units u ON u.unit_id = r.unit_id "
            "JOIN ukb_processing_versions p ON p.processing_id = r.processing_id "
            "WHERE r.unit_revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise KeyError(revision_id)
        return UnitTarget(*row)

    def unit_revision(self, revision_id: str) -> UnitTarget:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._unit_revision(db, revision_id)

    def unit(self, unit_id: str) -> UnitTarget:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT target_revision_id FROM ukb_import_units WHERE unit_id = ?",
                (unit_id,),
            ).fetchone()
            if not row or row[0] is None:
                raise KeyError(unit_id)
            return self._unit_revision(db, row[0])

    def unit_for_source(self, source_id: str, *, key: str) -> UnitTarget | None:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT target_revision_id FROM ukb_import_units "
                "WHERE source_id = ? AND unit_key = ?",
                (source_id, key),
            ).fetchone()
            return self._unit_revision(db, row[0]) if row and row[0] else None

    def source_units(self, source_id: str) -> tuple[UnitTarget, ...]:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            revisions = db.connection.execute(
                "SELECT target_revision_id FROM ukb_import_units WHERE source_id = ? "
                "AND target_revision_id IS NOT NULL ORDER BY unit_key",
                (source_id,),
            ).fetchall()
            return tuple(self._unit_revision(db, row[0]) for row in revisions)

    def unit_exclusion(self, revision_id: str):
        from .source_changes import worksheet_exclusion

        with storage_stage(self.root, self.identity, read_only=True) as db:
            return worksheet_exclusion(db.connection, revision_id)
