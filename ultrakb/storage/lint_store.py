"""Recover one fixed lint publication without repeating model work or repair consent."""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from ultrakb.compile_checkpoints import digest
from ultrakb.source_records import RelativePath

from .blobs import put_blob, read_bytes, retain_blob
from .coordination import storage_stage
from .execution_store import require_active_execution
from .knowledge_store import PreparedRevision


class LintDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structural_report: str
    knowledge_report: str | None
    report_path: RelativePath
    files_changed: int | None = Field(ge=0)
    ghosts_removed: int | None = Field(ge=0)
    changes: tuple[RelativePath, ...]
    quality: tuple[str, ...]


@dataclass(frozen=True)
class LintCandidate:
    prepared: PreparedRevision
    details: LintDetails
    sync_ready: bool


class LintStore:
    def __init__(self, root, identity):
        self.root, self.identity = root, identity

    @staticmethod
    def _key(base, model_budget_id, *, fix, semantic, unique):
        return digest(
            {
                "operation": "lint-v1",
                "publication_id": base.publication_id,
                "model_budget_id": model_budget_id,
                "view_id": base.publication.view_id,
                "fix": fix,
                "semantic": semantic,
                "unique": unique,
            }
        )

    def find(self, base, model_budget_id, **options):
        key = self._key(base, model_budget_id, **options)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            base._require_publication(db)
            row = db.connection.execute(
                "SELECT r.revision_id, r.publication_id, r.view_id, r.expected_publication, "
                "r.manifest_digest, r.navigation_fingerprint, s.plan_id, NULL, c.detail_digest, "
                "sp.base_generation = ps.target_generation AND sp.applied_epoch = ps.applied_epoch "
                "FROM ukb_lint_candidates c JOIN ukb_knowledge_revisions r "
                "ON r.revision_id = c.revision_id JOIN ukb_revision_sync s "
                "ON s.revision_id = r.revision_id JOIN ukb_sync_plans sp ON sp.plan_id = s.plan_id "
                "JOIN ukb_projection_state ps ON ps.view_id = r.view_id WHERE c.work_key = ? "
                "AND r.expected_publication = ? AND r.view_id = ? "
                "AND r.state IN ('ready', 'published') ORDER BY c.rowid DESC LIMIT 1",
                (key, base.publication_id, base.publication.view_id),
            ).fetchone()
            if row is None:
                return None
            return LintCandidate(
                PreparedRevision(*row[:8]),
                LintDetails.model_validate_json(read_bytes(db, row[8])),
                bool(row[9]),
            )

    def save(self, base, prepared, details, execution, **options):
        details = LintDetails.model_validate(details)
        key = self._key(base, execution.model_budget_id, **options)
        if prepared.expected_publication != base.publication_id:
            raise ValueError("Lint candidate belongs to another publication")
        encoded = details.model_dump_json().encode()
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("Lint result exceeds the storage payload limit")
        with storage_stage(self.root, self.identity) as db:
            blob = put_blob(db, encoded)
            with db.transaction() as connection:
                require_active_execution(connection, self.identity, execution.execution_id)
                inserted = connection.execute(
                    "INSERT INTO ukb_lint_candidates VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(revision_id) DO NOTHING",
                    (prepared.revision_id, key, execution.execution_id, blob.digest),
                ).rowcount
                if inserted:
                    retain_blob(connection, "lint-candidate", prepared.revision_id, "details", blob)
        return self.find(base, execution.model_budget_id, **options)
