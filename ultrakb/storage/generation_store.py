"""Retained output plans and exact artifact acceptance for generator recovery."""

import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ultrakb.compile_checkpoints import digest
from ultrakb.mutation import RecoveryRequired
from ultrakb.source_records import RelativePath

from .artifact_store import ArtifactRevision, ArtifactStore, artifact_path
from .blobs import put_blob, read_bytes, retain_blob
from .coordination import storage_stage
from .execution_store import require_active_execution
from .knowledge_store import KnowledgeStore, PreparedRevision, _manifest
from .wiki_paths import wiki_path


class GenerationDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_type: Literal["skill", "deck"]
    output_dir: RelativePath
    artifact_path: RelativePath
    validation_errors: tuple[str, ...] = ()
    validation_warnings: tuple[str, ...] = ()
    validation_known: bool = True
    generation_complete: bool = True
    error_type: str | None = None
    unfinished: tuple[str, ...] = ()
    archive_path: RelativePath | None = None
    quality: tuple[str, ...] = ()
    knowledge_changes: tuple[tuple[RelativePath, Literal["write", "delete"]], ...] = ()


@dataclass(frozen=True)
class GenerationPlan:
    plan_id: str
    model_budget_id: str
    publication_id: str
    expected_epoch: int
    details: GenerationDetails
    prepared: PreparedRevision | None = None


@dataclass(frozen=True)
class GenerationAcceptance:
    files: tuple[ArtifactRevision, ...]
    conflict: ArtifactRevision | None = None
    error: Exception | None = None


def generation_request(scope, options):
    return digest(
        {
            "operation": "generate-v1",
            "publication_id": scope.publication_id,
            "view_id": scope.view_id,
            "target_type": options.target_type,
            "name": options.name,
            "intent_sha256": digest(options.intent),
            "overwrite": options.overwrite,
            "version": options.version,
            "critique": options.critique,
            "skill_name": options.skill_name,
        }
    )


class GenerationStore:
    def __init__(self, root, identity):
        self.root, self.identity = root, identity

    def find(self, model_budget_id, request_key):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT plan_id, model_budget_id, publication_id, expected_epoch, detail_digest, "
                "knowledge_revision_id "
                "FROM ukb_generation_plans WHERE model_budget_id = ? AND request_key = ? "
                "AND state = 'ready' ORDER BY rowid DESC LIMIT 1",
                (model_budget_id, request_key),
            ).fetchone()
            if row is None:
                return None
            prepared = None
            if row[5] is not None:
                revision = db.connection.execute(
                    "SELECT r.revision_id, r.publication_id, r.view_id, r.expected_publication, "
                    "r.manifest_digest, r.navigation_fingerprint, s.plan_id "
                    "FROM ukb_knowledge_revisions r JOIN ukb_revision_sync s "
                    "ON s.revision_id = r.revision_id WHERE r.revision_id = ? "
                    "AND r.state IN ('ready', 'published')",
                    (row[5],),
                ).fetchone()
                if revision is None:
                    raise RecoveryRequired("The generator knowledge candidate is unavailable")
                prepared = PreparedRevision(*revision)
            return GenerationPlan(
                *row[:4], GenerationDetails.model_validate_json(read_bytes(db, row[4])), prepared
            )

    def prepare(
        self,
        work,
        request_key,
        publication_id,
        epoch,
        details,
        files,
        *,
        knowledge_base=None,
        knowledge_inputs=None,
    ):
        if (work.root, work.identity) != (self.root, self.identity):
            raise ValueError("Generator work belongs to another database")
        details = GenerationDetails.model_validate(details)
        knowledge_files = {}
        for path, payload in files.items():
            wiki_path(path)
            if payload is not None and not isinstance(payload, bytes):
                raise ValueError("Generator outputs require immutable bytes")
            if path.startswith("wiki/"):
                if not path.startswith("wiki/explorations/"):
                    raise ValueError("Generated knowledge requires exploration files")
                knowledge_files[path.removeprefix("wiki/")] = payload
        details = details.model_copy(
            update={
                "knowledge_changes": tuple(
                    (path, "delete" if payload is None else "write")
                    for path, payload in sorted(knowledge_files.items())
                )
            }
        )
        prepared = None
        if knowledge_files:
            knowledge = KnowledgeStore(self.root, self.identity)
            base = knowledge.bind(knowledge_base or publication_id)
            units = tuple(unit.unit_revision_id for unit in base.input_units())
            basis = {}
            for path, payload in knowledge_files.items():
                if payload is None or not path.endswith(".md"):
                    continue
                try:
                    unchanged = base.file_digest(path) == hashlib.sha256(payload).hexdigest()
                except FileNotFoundError:
                    unchanged = False
                basis[path] = (
                    tuple(unit.unit_revision_id for unit in base.input_units(page=path))
                    if unchanged
                    else units
                )
            prepared = knowledge.prepare_changes(
                base,
                knowledge_files,
                page_inputs=basis | (knowledge_inputs or {}),
            )
        plan_id = uuid4().hex
        entries = []
        ordered = sorted(
            ((path, payload) for path, payload in files.items() if not path.startswith("wiki/")),
            key=lambda item: (
                item[1] is None,
                item[0] == ".claude-plugin/marketplace.json",
                item[0],
            ),
        )
        for ordinal, (path, payload) in enumerate(ordered):
            artifact_path(path)
            if payload is not None and not isinstance(payload, bytes):
                raise ValueError("Generator outputs require immutable bytes")
            entries.append(
                (
                    ordinal,
                    path,
                    uuid4().hex,
                    hashlib.sha256(payload or b"").hexdigest(),
                    len(payload or b""),
                    "delete" if payload is None else "write",
                )
            )
        encoded = details.model_dump_json().encode()
        if len(encoded) > 4 * 1024 * 1024 or epoch + len(entries) >= 2**63:
            raise ValueError("Generator plan exceeds the storage limits")
        manifest = hashlib.sha256(_manifest(entries)).hexdigest()
        with storage_stage(self.root, self.identity) as db:
            blob = put_blob(db, encoded)
            with db.transaction() as connection:
                work._active(connection)
                connection.execute(
                    "INSERT INTO ukb_generation_plans VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'building')",
                    (
                        plan_id,
                        work.work_id,
                        request_key,
                        work.model_budget_id,
                        work.execution_id,
                        publication_id,
                        epoch,
                        blob.digest,
                        len(entries),
                        manifest,
                        prepared.revision_id if prepared is not None else None,
                    ),
                )
                retain_blob(connection, "generator-plan", plan_id, "details", blob)
        for ordinal, path, revision_id, expected_digest, size, operation in entries:
            with storage_stage(self.root, self.identity) as db:
                blob = put_blob(db, files[path] if files[path] is not None else b"")
                if (blob.digest, blob.byte_count) != (expected_digest, size):
                    raise ValueError("Generator output changed during preparation")
                with db.transaction() as connection:
                    work._active(connection)
                    connection.execute(
                        "INSERT INTO ukb_generation_files VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (plan_id, ordinal, path, revision_id, blob.digest, size, operation),
                    )
                    retain_blob(connection, "generator-plan", plan_id, path, blob)
        with storage_stage(self.root, self.identity) as db:
            rows = db.connection.execute(
                "SELECT ordinal, path, revision_id, digest, byte_count, operation "
                "FROM ukb_generation_files "
                "WHERE plan_id = ? ORDER BY ordinal",
                (plan_id,),
            ).fetchall()
            if rows != entries or hashlib.sha256(_manifest(rows)).hexdigest() != manifest:
                raise ValueError("Generator manifest is incomplete")
            with db.transaction() as connection:
                work._active(connection)
                connection.execute(
                    "UPDATE ukb_generation_plans SET state = 'ready' WHERE plan_id = ?", (plan_id,)
                )
        return self.find(work.model_budget_id, request_key)

    def archive(self, plan):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT request_key FROM ukb_generation_plans WHERE plan_id = ?", (plan.plan_id,)
            ).fetchone()
        if row is None:
            raise RecoveryRequired("The retained generator plan is unavailable")
        archive_key = digest({"operation": "generator-archive-v1", "request": row[0]})
        return self.find(plan.model_budget_id, archive_key)

    def accept(self, plan, execution_id, *, check_cancelled):
        accepted = []
        try:
            return self._accept_files(plan, execution_id, check_cancelled, accepted)
        except Exception as error:
            # These exact receipts have already returned. Later I/O cannot revoke them,
            # and recovering them must not depend on the same failing database.
            return GenerationAcceptance(tuple(accepted), error=error)

    def _accept_files(self, plan, execution_id, check_cancelled, accepted):
        artifacts = ArtifactStore(self.root, self.identity)
        if execution_id is not None:
            with storage_stage(self.root, self.identity, read_only=True) as db:
                require_active_execution(db.connection, self.identity, execution_id)
                if db.connection.execute(
                    "SELECT model_budget_id FROM ukb_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone() != (plan.model_budget_id,):
                    raise ValueError("Generator plan belongs to another model budget")
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if db.connection.execute(
                "SELECT state FROM ukb_generation_plans WHERE plan_id = ?",
                (plan.plan_id,),
            ).fetchone() != ("ready",):
                raise RecoveryRequired("The generator plan is no longer available for acceptance")
            rows = db.connection.execute(
                "SELECT ordinal, path, revision_id, digest, operation FROM ukb_generation_files "
                "WHERE plan_id = ? ORDER BY ordinal",
                (plan.plan_id,),
            ).fetchall()
        for ordinal, path, revision_id, content_digest, operation in rows:
            with storage_stage(self.root, self.identity, read_only=True) as db:
                payload = read_bytes(db, content_digest)
            fields = dict(
                path=path,
                payload=None if operation == "delete" else payload,
                expected_epoch=plan.expected_epoch + ordinal,
                evidence_publication_id=plan.publication_id,
            )
            revision = artifacts.resumed_write(
                revision_id, model_budget_id=plan.model_budget_id, **fields
            )
            if revision is None:
                if execution_id is None:
                    raise ValueError("Unaccepted generator files require an active execution")
                try:
                    revision = artifacts.save(
                        **fields,
                        execution_id=execution_id,
                        revision_id=revision_id,
                        check_cancelled=check_cancelled,
                    )
                except Exception:
                    revision = artifacts.resumed_write(
                        revision_id, model_budget_id=plan.model_budget_id, **fields
                    )
                    if revision is None:
                        raise
            if revision.status == "conflict":
                return GenerationAcceptance(tuple(accepted), conflict=revision)
            accepted.append(revision)
        return GenerationAcceptance(tuple(accepted))

    def has_pending(self, plan):
        if (
            plan.prepared is not None
            and KnowledgeStore(self.root, self.identity).committed_publication(plan.prepared)
            is None
        ):
            return True
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return (
                db.connection.execute(
                    "SELECT 1 FROM ukb_generation_files f LEFT JOIN ukb_artifact_revisions r "
                    "ON r.revision_id = f.revision_id WHERE f.plan_id = ? "
                    "AND r.revision_id IS NULL LIMIT 1",
                    (plan.plan_id,),
                ).fetchone()
                is not None
            )
