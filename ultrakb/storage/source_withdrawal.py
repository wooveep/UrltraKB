"""A source exclusion is sealed and committed with its exact knowledge publication."""

from pydantic import BaseModel, ConfigDict, Field

from ultrakb.source_records import RecordId

from .catalog_store import CatalogConflict
from .coordination import storage_stage


class SourceWithdrawal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: RecordId
    source_revision_id: RecordId
    source_generation: int = Field(ge=0, lt=2**63, strict=True)


def require_source(connection, claim):
    source = connection.execute(
        "SELECT state, generation, (SELECT revision_id FROM ukb_source_revisions r "
        "WHERE r.source_id = s.source_id AND r.state = 'ready' ORDER BY ordinal DESC LIMIT 1) "
        "FROM ukb_sources s WHERE source_id = ?",
        (claim.source_id,),
    ).fetchone()
    if source != ("active", claim.source_generation, claim.source_revision_id):
        raise CatalogConflict("The reviewed source or processing targets changed")


def read_withdrawal(connection, revision_id):
    row = connection.execute(
        "SELECT source_id, source_revision_id, source_generation FROM ukb_revision_withdrawals "
        "WHERE revision_id = ?",
        (revision_id,),
    ).fetchone()
    return (
        SourceWithdrawal(source_id=row[0], source_revision_id=row[1], source_generation=row[2])
        if row
        else None
    )


def withdrawal_manifest(connection, revision_id):
    claim = read_withdrawal(connection, revision_id)
    return claim.model_dump(mode="json") if claim else None


def finish_withdrawal(connection, revision_id):
    claim = read_withdrawal(connection, revision_id)
    if claim is None:
        return
    if not connection.in_transaction:
        raise ValueError("Source withdrawal belongs to the publication transaction")
    require_source(connection, claim)
    connection.execute(
        "UPDATE ukb_sources SET state = 'withdrawn', generation = generation + 1 "
        "WHERE source_id = ? AND generation = ? AND state = 'active'",
        (claim.source_id, claim.source_generation),
    )


def committed_withdrawal(store, base, source_id):
    """Recover an exact publication after a lost acknowledgement, including later heads."""
    from .knowledge_store import PreparedRevision

    if base.root != store.root or base.identity != store.identity:
        raise ValueError("Withdrawal recovery requires this exact database baseline")
    with storage_stage(store.root, store.identity, read_only=True) as db:
        row = db.connection.execute(
            "SELECT r.revision_id, r.publication_id, r.view_id, r.expected_publication, "
            "r.manifest_digest, r.navigation_fingerprint, s.plan_id, c.attempt_id, "
            "w.source_revision_id, w.source_generation "
            "FROM ukb_revision_withdrawals w JOIN ukb_knowledge_revisions r "
            "ON r.revision_id = w.revision_id JOIN ukb_publication_receipts c "
            "ON c.revision_id = r.revision_id AND c.publication_id = r.publication_id "
            "JOIN ukb_revision_sync s ON s.revision_id = r.revision_id "
            "WHERE w.source_id = ? AND r.expected_publication = ? AND r.view_id = ? "
            "AND c.generation = ? AND r.state = 'published'",
            (source_id, base.publication_id, base.publication.view_id, store.identity.generation),
        ).fetchone()
        if row is None:
            return None
        return PreparedRevision(*row[:8]), SourceWithdrawal(
            source_id=source_id, source_revision_id=row[8], source_generation=row[9]
        )
