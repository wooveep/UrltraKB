"""A source exclusion is sealed and committed with its exact knowledge publication."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ultrakb.source_records import RecordId

from .catalog_store import CatalogConflict
from .coordination import storage_stage


class SourceChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: RecordId
    source_revision_id: RecordId
    source_generation: int = Field(ge=0, lt=2**63, strict=True)
    kind: Literal["withdrawn", "empty"] = "withdrawn"


def require_source(connection, claim):
    source = connection.execute(
        "SELECT state, generation, (SELECT revision_id FROM ukb_source_revisions r "
        "WHERE r.source_id = s.source_id AND r.state = 'ready' ORDER BY ordinal DESC LIMIT 1), "
        "contribution_empty "
        "FROM ukb_sources s WHERE source_id = ?",
        (claim.source_id,),
    ).fetchone()
    if (
        source is None
        or source[:3] != ("active", claim.source_generation, claim.source_revision_id)
        or claim.kind == "empty"
        and source[3]
    ):
        raise CatalogConflict("The reviewed source or processing targets changed")


def read_source_change(connection, revision_id):
    row = connection.execute(
        "SELECT source_id, source_revision_id, source_generation, kind "
        "FROM ukb_revision_source_changes "
        "WHERE revision_id = ?",
        (revision_id,),
    ).fetchone()
    return (
        SourceChange(
            source_id=row[0], source_revision_id=row[1], source_generation=row[2], kind=row[3]
        )
        if row
        else None
    )


def source_change_manifest(connection, revision_id):
    claim = read_source_change(connection, revision_id)
    return claim.model_dump(mode="json") if claim else None


def finish_source_change(connection, revision_id):
    claim = read_source_change(connection, revision_id)
    if claim is None:
        return
    if not connection.in_transaction:
        raise ValueError("Source contribution changes belong to the publication transaction")
    require_source(connection, claim)
    connection.execute(
        "UPDATE ukb_sources SET state = ?, contribution_empty = ?, generation = generation + 1 "
        "WHERE source_id = ? AND generation = ? AND state = 'active'",
        (
            "withdrawn" if claim.kind == "withdrawn" else "active",
            claim.kind == "empty",
            claim.source_id,
            claim.source_generation,
        ),
    )


def committed_source_change(store, base, source_id, *, kind="withdrawn", source_generation=None):
    """Recover an exact publication after a lost acknowledgement, including later heads."""
    from .knowledge_store import PreparedRevision

    if base.root != store.root or base.identity != store.identity:
        raise ValueError("Source change recovery requires this exact database baseline")
    if (
        kind not in {"withdrawn", "empty"}
        or source_generation is not None
        and (type(source_generation) is not int or not 0 <= source_generation < 2**63)
    ):
        raise ValueError("Source change recovery requires an exact kind and source generation")
    with storage_stage(store.root, store.identity, read_only=True) as db:
        row = db.connection.execute(
            "SELECT r.revision_id, r.publication_id, r.view_id, r.expected_publication, "
            "r.manifest_digest, r.navigation_fingerprint, s.plan_id, c.attempt_id, "
            "w.source_revision_id, w.source_generation, w.kind "
            "FROM ukb_revision_source_changes w JOIN ukb_knowledge_revisions r "
            "ON r.revision_id = w.revision_id JOIN ukb_publication_receipts c "
            "ON c.revision_id = r.revision_id AND c.publication_id = r.publication_id "
            "JOIN ukb_revision_sync s ON s.revision_id = r.revision_id "
            "WHERE w.source_id = ? AND w.kind = ? AND (? OR r.expected_publication = ?) "
            "AND r.view_id = ? AND (? IS NULL OR w.source_generation = ?) "
            "AND c.generation = ? AND r.state = 'published'",
            (
                source_id,
                kind,
                source_generation is not None,
                base.publication_id,
                base.publication.view_id,
                source_generation,
                source_generation,
                store.identity.generation,
            ),
        ).fetchone()
        if row is None:
            return None
        return PreparedRevision(*row[:8]), SourceChange(
            source_id=source_id, source_revision_id=row[8], source_generation=row[9], kind=row[10]
        )
