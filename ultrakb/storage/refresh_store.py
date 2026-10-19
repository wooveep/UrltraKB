"""Retained refresh candidates, exact review tokens and catalog target snapshots."""

import hashlib
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, TypeAdapter

from ultrakb.source_records import RecordId, RelativePath

from .blobs import put_blob, read_bytes, retain_blob
from .catalog_store import CatalogStore
from .coordination import storage_stage
from .execution_store import require_active_execution
from .knowledge_store import PreparedRevision, PublicationConflict, _manifest


class RefreshDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    conflicts: tuple[RelativePath, ...]
    remaining: tuple[RelativePath, ...] = ()


@dataclass(frozen=True)
class RefreshTargets:
    generation: int
    units: tuple
    blocked: tuple[str, ...]


def refresh_targets(bound):
    with storage_stage(bound.root, bound.identity, read_only=True) as db:
        db.connection.execute("BEGIN")
        bound._require_publication(db)
        if db.connection.execute(
            "SELECT publication_id FROM ukb_heads WHERE view_id = ?",
            (bound.publication.view_id,),
        ).fetchone() != (bound.publication_id,):
            raise PublicationConflict("Knowledge changed before refresh")
        [generation] = db.connection.execute(
            "SELECT generation FROM ukb_catalog_state WHERE singleton = 1"
        ).fetchone()
        active = {
            row[0]
            for row in db.connection.execute(
                "SELECT unit_revision_id FROM ukb_revision_inputs WHERE revision_id = ?",
                (bound.publication.revision_id,),
            ).fetchall()
        }
        targets, blocked = [], []
        for source, label, revision in db.connection.execute(
            "SELECT s.source_id, s.label, (SELECT revision_id FROM ukb_source_revisions r "
            "WHERE r.source_id = s.source_id AND r.state = 'ready' ORDER BY ordinal DESC LIMIT 1) "
            "FROM ukb_sources s WHERE state = 'active' AND contribution_empty = 0 "
            "ORDER BY source_id"
        ).fetchall():
            units = db.connection.execute(
                "SELECT target_revision_id FROM ukb_import_units "
                "WHERE source_id = ? ORDER BY unit_key",
                (source,),
            ).fetchall()
            if not units:
                blocked.append(label)
            for [unit] in units:
                target = CatalogStore._unit_revision(db, unit)
                from .source_changes import worksheet_exclusion

                if target.source_revision_id == revision and worksheet_exclusion(
                    db.connection, unit
                ):
                    continue
                if target.source_revision_id != revision or unit not in active:
                    blocked.append(label)
                else:
                    targets.append(target)
        return RefreshTargets(generation, tuple(targets), tuple(dict.fromkeys(blocked)))


class RefreshStore:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def _work_key(self, publication_id, model_budget_id, selection="full"):
        TypeAdapter(RecordId).validate_python(publication_id)
        TypeAdapter(RecordId).validate_python(model_budget_id)
        return hashlib.sha256(
            _manifest((self.identity.generation, publication_id, model_budget_id, selection))
        ).hexdigest()

    def find(self, publication_id, model_budget_id, *, selection="full"):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT p.proposal_id FROM ukb_refresh_proposals p "
                "JOIN ukb_knowledge_revisions r ON r.revision_id = p.proposal_id "
                "JOIN ukb_executions e ON e.execution_id = p.execution_id "
                "WHERE p.work_key = ? AND r.expected_publication = ? AND e.model_budget_id = ? "
                "ORDER BY p.rowid DESC LIMIT 1",
                (
                    self._work_key(publication_id, model_budget_id, selection),
                    publication_id,
                    model_budget_id,
                ),
            ).fetchone()
        return self.read(row[0]) if row else None

    def save(self, prepared, *, conflicts, execution, selection="full", remaining=()):
        details = RefreshDetails(
            conflicts=tuple(sorted(set(conflicts))), remaining=tuple(remaining)
        )
        encoded = details.model_dump_json().encode()
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("Refresh review metadata exceeds the storage payload limit")
        key = self._work_key(prepared.expected_publication, execution.model_budget_id, selection)
        with storage_stage(self.root, self.identity) as db:
            blob = put_blob(db, encoded)
            with db.transaction() as connection:
                require_active_execution(connection, self.identity, execution.execution_id)
                if connection.execute(
                    "SELECT state FROM ukb_knowledge_revisions WHERE revision_id = ?",
                    (prepared.revision_id,),
                ).fetchone() != ("ready",):
                    raise ValueError("Refresh review requires its exact ready candidate")
                connection.execute(
                    "INSERT INTO ukb_refresh_proposals VALUES (?, ?, ?, ?, ?)",
                    (
                        prepared.revision_id,
                        key,
                        blob.digest,
                        execution.execution_id,
                        len(details.conflicts),
                    ),
                )
                retain_blob(connection, "refresh-proposal", prepared.revision_id, "details", blob)
        return self.read(prepared.revision_id)

    def read(self, proposal_id):
        TypeAdapter(RecordId).validate_python(proposal_id)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            db.connection.execute("BEGIN")
            row = db.connection.execute(
                "SELECT r.revision_id, r.publication_id, r.view_id, r.expected_publication, "
                "r.manifest_digest, r.navigation_fingerprint, s.plan_id, NULL, "
                "f.catalog_generation, p.detail_digest, p.conflict_count "
                "FROM ukb_refresh_proposals p JOIN ukb_knowledge_revisions r "
                "ON r.revision_id = p.proposal_id JOIN ukb_revision_sync s "
                "ON s.revision_id = r.revision_id JOIN ukb_revision_refresh f "
                "ON f.revision_id = r.revision_id WHERE p.proposal_id = ? "
                "AND r.state IN ('ready', 'published')",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("No retained refresh candidate")
            details = RefreshDetails.model_validate_json(read_bytes(db, row[9]))
            if len(details.conflicts) != row[10]:
                raise ValueError("Refresh conflict metadata changed")
            prepared = PreparedRevision(*row[:8])
            current = db.connection.execute(
                "SELECT h.publication_id, c.generation FROM ukb_heads h, ukb_catalog_state c "
                "WHERE h.view_id = ? AND c.singleton = 1",
                (prepared.view_id,),
            ).fetchone() == (prepared.expected_publication, row[8])
            published = (
                db.connection.execute(
                    "SELECT 1 FROM ukb_publication_receipts WHERE revision_id = ? "
                    "AND publication_id = ?",
                    (prepared.revision_id, prepared.publication_id),
                ).fetchone()
                is not None
            )
            sync_current = (
                db.connection.execute(
                    "SELECT 1 FROM ukb_sync_plans s JOIN ukb_projection_state p "
                    "ON p.view_id = s.view_id WHERE s.plan_id = ? "
                    "AND s.base_generation = p.target_generation "
                    "AND s.applied_epoch = p.applied_epoch",
                    (prepared.sync_plan_id,),
                ).fetchone()
                is not None
            )
            version = hashlib.sha256(
                _manifest((self.identity.kb_id, self.identity.generation, tuple(row[:10])))
            ).hexdigest()
            return {
                "proposal_id": proposal_id,
                "view_id": prepared.view_id,
                "version": version,
                "conflicts": details.conflicts,
                "remaining": details.remaining,
                "catalog_generation": row[8],
                "prepared": prepared,
                "content_current": current,
                "sync_ready": sync_current,
                "status": "accepted"
                if published
                else "awaiting_confirmation"
                if current and sync_current
                else "superseded",
            }

    def summaries(self, view_id, *, limit=63):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Refresh list requires a bounded positive limit")
        with storage_stage(self.root, self.identity, read_only=True) as db:
            rows = db.connection.execute(
                "SELECT p.proposal_id, p.conflict_count, r.publication_id, "
                "r.expected_publication, f.catalog_generation, h.publication_id, c.generation, "
                "r.state, s.base_generation = ps.target_generation "
                "AND s.applied_epoch = ps.applied_epoch "
                "FROM ukb_refresh_proposals p JOIN ukb_knowledge_revisions r "
                "ON r.revision_id = p.proposal_id JOIN ukb_revision_refresh f "
                "ON f.revision_id = r.revision_id JOIN ukb_heads h ON h.view_id = r.view_id "
                "JOIN ukb_revision_sync rs ON rs.revision_id = r.revision_id "
                "JOIN ukb_sync_plans s ON s.plan_id = rs.plan_id "
                "JOIN ukb_projection_state ps ON ps.view_id = r.view_id, "
                "ukb_catalog_state c WHERE r.view_id = ? AND c.singleton = 1 "
                "AND (p.conflict_count > 0 OR r.state != 'published') "
                "ORDER BY p.rowid DESC LIMIT ?",
                (view_id, limit),
            ).fetchall()
            return tuple(
                {
                    "proposal_id": row[0],
                    "view_id": view_id,
                    "conflict_count": row[1],
                    "publication_id": row[2],
                    "status": "accepted"
                    if row[7] == "published"
                    else "awaiting_confirmation"
                    if row[3:5] == row[5:7] and row[8]
                    else "superseded",
                }
                for row in rows
            )

    def files(self, proposal_id):
        TypeAdapter(RecordId).validate_python(proposal_id)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return dict(
                db.connection.execute(
                    "SELECT f.path, f.digest FROM ukb_revision_files f "
                    "JOIN ukb_refresh_proposals p ON p.proposal_id = f.revision_id "
                    "JOIN ukb_knowledge_revisions r ON r.revision_id = p.proposal_id "
                    "WHERE r.revision_id = ? AND r.state IN ('ready', 'published') ORDER BY path",
                    (proposal_id,),
                ).fetchall()
            )

    def read_file(self, proposal_id, path):
        from .wiki_paths import wiki_path

        wiki_path(path)
        TypeAdapter(RecordId).validate_python(proposal_id)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT f.digest FROM ukb_revision_files f JOIN ukb_refresh_proposals p "
                "ON p.proposal_id = f.revision_id JOIN ukb_knowledge_revisions r "
                "ON r.revision_id = p.proposal_id WHERE r.revision_id = ? AND f.path = ? "
                "AND r.state IN ('ready', 'published')",
                (proposal_id, path),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(path)
            return read_bytes(db, row[0])
