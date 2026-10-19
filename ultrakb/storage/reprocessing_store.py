"""Reviewed reprocessing requests keep their input, policy and retry budget identity."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ultrakb.compile_checkpoints import digest
from ultrakb.source_records import Digest, RecordId, ViewId

from .blobs import put_blob, read_bytes, retain_blob
from .catalog_store import CatalogConflict, CatalogStore
from .coordination import storage_stage


class ReprocessingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[2] = 2
    kb_id: RecordId
    generation: str = Field(min_length=1)
    source_id: RecordId
    source_revision_id: RecordId
    source_generation: int = Field(ge=0, lt=2**63, strict=True)
    view_id: ViewId
    publication_id: RecordId
    units: dict[RecordId, RecordId]
    policy: dict
    previous_request_id: Digest | None = None

    @property
    def request_id(self):
        return digest(self.model_dump(mode="json"))


class ReprocessingStore:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def read(self, request_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT payload_digest FROM ukb_reprocessing_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            plan = ReprocessingPlan.model_validate_json(read_bytes(db, row[0]))
            if plan.request_id != request_id or plan.kb_id != self.identity.kb_id:
                raise ValueError("Reprocessing request identity differs from its payload")
            return plan

    def latest(self, source_revision_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT request_id FROM ukb_reprocessing_requests WHERE source_revision_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (source_revision_id,),
            ).fetchone()
        return self.read(row[0]) if row else None

    def retain(self, plan):
        if (plan.kb_id, plan.generation) != (self.identity.kb_id, self.identity.generation):
            raise CatalogConflict("Reprocessing preview belongs to another database generation")
        payload = plan.model_dump_json().encode()
        request_id = plan.request_id
        with storage_stage(self.root, self.identity) as db:
            content = put_blob(db, payload)
            with db.transaction() as connection:
                current = CatalogStore._latest(db, plan.source_id)
                if not current or current[0] != plan.source_revision_id:
                    raise CatalogConflict("Source changed after reprocessing preview")
                latest = connection.execute(
                    "SELECT request_id FROM ukb_reprocessing_requests WHERE source_revision_id = ? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (plan.source_revision_id,),
                ).fetchone()
                if (latest[0] if latest else None) not in {plan.previous_request_id, request_id}:
                    raise CatalogConflict("Reprocessing target changed; refresh the preview")
                if latest != (request_id,) and connection.execute(
                    "SELECT state, generation FROM ukb_sources WHERE source_id = ?",
                    (plan.source_id,),
                ).fetchone() != ("active", plan.source_generation):
                    raise CatalogConflict("Source contribution changed after reprocessing preview")
                if latest == (request_id,):
                    require_current_reprocessing(connection, plan.source_revision_id, request_id)
                connection.execute(
                    "INSERT INTO ukb_reprocessing_requests "
                    "(request_id, source_id, source_revision_id, payload_digest, execution_id, "
                    "source_generation, expected_source_generation) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?) "
                    "ON CONFLICT(request_id) DO NOTHING",
                    (
                        request_id,
                        plan.source_id,
                        plan.source_revision_id,
                        content.digest,
                        plan.source_generation,
                        plan.source_generation,
                    ),
                )
                stored = connection.execute(
                    "SELECT payload_digest FROM ukb_reprocessing_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()[0]
                if stored != content.digest:
                    raise CatalogConflict("Reprocessing request already accepted another plan")
                retain_blob(connection, "reprocessing-request", request_id, "plan", content)
        return plan

    def execution(self, request_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT (SELECT latest.execution_id FROM ukb_executions latest "
                "WHERE latest.task_id = original.task_id ORDER BY latest.sequence DESC LIMIT 1) "
                "FROM ukb_reprocessing_requests r LEFT JOIN ukb_executions original "
                "ON original.execution_id = r.execution_id WHERE r.request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            return row[0]

    def check_publication(self, plan, publication_id):
        """A retry may follow its own worksheet publications, never another writer."""
        current, seen = publication_id, set()
        with storage_stage(self.root, self.identity, read_only=True) as db:
            require_current_reprocessing(db.connection, plan.source_revision_id, plan.request_id)
            while current != plan.publication_id:
                if current in seen:
                    raise CatalogConflict("Invalid reprocessing publication ancestry")
                seen.add(current)
                row = db.connection.execute(
                    "SELECT k.expected_publication, i.source_revision_id, "
                    "p.reprocessing_request_id "
                    "FROM ukb_publications pub "
                    "JOIN ukb_knowledge_revisions k ON k.revision_id = pub.revision_id "
                    "LEFT JOIN ukb_revision_import i ON i.revision_id = k.revision_id "
                    "LEFT JOIN ukb_unit_revisions u ON u.unit_revision_id = i.unit_revision_id "
                    "LEFT JOIN ukb_processing_versions p ON p.processing_id = u.processing_id "
                    "WHERE pub.publication_id = ? AND pub.view_id = ?",
                    (current, plan.view_id),
                ).fetchone()
                if (
                    row is None
                    or row[0] is None
                    or row[1:] != (plan.source_revision_id, plan.request_id)
                ):
                    raise CatalogConflict(
                        "Knowledge publication changed; preview reprocessing again"
                    )
                current = row[0]

    def bind_execution(self, request_id, execution):
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            previous = connection.execute(
                "SELECT execution_id FROM ukb_reprocessing_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if previous is None:
                raise KeyError(request_id)
            current = connection.execute(
                "SELECT task_id, model_budget_id, generation, state FROM ukb_executions "
                "WHERE execution_id = ?",
                (execution.execution_id,),
            ).fetchone()
            if current != (
                execution.task_id,
                execution.model_budget_id,
                self.identity.generation,
                "started",
            ):
                raise ValueError("Reprocessing requires its actual active execution")
            if previous[0] is not None:
                old = connection.execute(
                    "SELECT task_id, model_budget_id FROM ukb_executions WHERE execution_id = ?",
                    (previous[0],),
                ).fetchone()
                if old != current[:2]:
                    raise ValueError("Reprocessing cannot reset its retained task or budget")
            connection.execute(
                "UPDATE ukb_reprocessing_requests SET execution_id = ? WHERE request_id = ?",
                (execution.execution_id, request_id),
            )


def require_current_reprocessing(connection, source_revision_id, request_id):
    if request_id is None:
        return
    current = connection.execute(
        "SELECT r.request_id, r.expected_source_generation, s.generation, s.state "
        "FROM ukb_reprocessing_requests r JOIN ukb_sources s ON s.source_id = r.source_id "
        "WHERE r.source_revision_id = ? ORDER BY r.sequence DESC LIMIT 1",
        (source_revision_id,),
    ).fetchone()
    if (
        current is None
        or current[0] != request_id
        or current[1] != current[2]
        or current[3] != "active"
    ):
        raise CatalogConflict("Reprocessing target changed; refresh the preview")
