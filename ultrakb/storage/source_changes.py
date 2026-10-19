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
    unit_revision_id: RecordId | None = None


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
        or claim.unit_revision_id is None
        and claim.kind == "empty"
        and source[3]
    ):
        raise CatalogConflict("The reviewed source or processing targets changed")
    if claim.unit_revision_id is not None:
        unit = connection.execute(
            "SELECT u.source_id, p.source_revision_id, p.content_state, u.unit_key, "
            "p.reprocessing_request_id "
            "FROM ukb_import_units u JOIN ukb_unit_revisions v "
            "ON v.unit_revision_id = u.target_revision_id "
            "JOIN ukb_processing_versions p ON p.processing_id = v.processing_id "
            "WHERE v.unit_revision_id = ? AND p.state = 'ready'",
            (claim.unit_revision_id,),
        ).fetchone()
        if (
            unit is None
            or unit[:3] != (claim.source_id, claim.source_revision_id, claim.kind)
            or unit[3] == "body"
            or source[3]
        ):
            raise CatalogConflict("The reviewed worksheet target changed")
        from .reprocessing_store import require_current_reprocessing

        require_current_reprocessing(connection, claim.source_revision_id, unit[4])


def read_source_change(connection, revision_id):
    row = connection.execute(
        "SELECT source_id, source_revision_id, source_generation, kind, unit_revision_id "
        "FROM ukb_revision_source_changes "
        "WHERE revision_id = ?",
        (revision_id,),
    ).fetchone()
    return (
        SourceChange(
            source_id=row[0],
            source_revision_id=row[1],
            source_generation=row[2],
            kind=row[3],
            unit_revision_id=row[4],
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
    if claim.unit_revision_id is not None:
        connection.execute(
            "UPDATE ukb_sources SET generation = generation + 1 WHERE source_id = ?",
            (claim.source_id,),
        )
        connection.execute(
            "UPDATE ukb_reprocessing_requests "
            "SET expected_source_generation = expected_source_generation + 1 "
            "WHERE request_id = (SELECT p.reprocessing_request_id FROM ukb_unit_revisions v "
            "JOIN ukb_processing_versions p ON p.processing_id = v.processing_id "
            "WHERE v.unit_revision_id = ?)",
            (claim.unit_revision_id,),
        )
        return
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


def committed_source_change(
    store, base, source_id, *, kind="withdrawn", source_generation=None, unit_revision_id=None
):
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
            "w.source_revision_id, w.source_generation, w.kind, w.unit_revision_id "
            "FROM ukb_revision_source_changes w JOIN ukb_knowledge_revisions r "
            "ON r.revision_id = w.revision_id JOIN ukb_publication_receipts c "
            "ON c.revision_id = r.revision_id AND c.publication_id = r.publication_id "
            "JOIN ukb_revision_sync s ON s.revision_id = r.revision_id "
            "WHERE w.source_id = ? AND w.kind = ? AND (? OR r.expected_publication = ?) "
            "AND r.view_id = ? AND (? IS NULL OR w.source_generation = ?) "
            "AND w.unit_revision_id IS ? "
            "AND c.generation = ? AND r.state = 'published'",
            (
                source_id,
                kind,
                source_generation is not None or unit_revision_id is not None,
                base.publication_id,
                base.publication.view_id,
                source_generation,
                source_generation,
                unit_revision_id,
                store.identity.generation,
            ),
        ).fetchone()
        if row is None:
            return None
        return PreparedRevision(*row[:8]), SourceChange(
            source_id=source_id,
            source_revision_id=row[8],
            source_generation=row[9],
            kind=row[10],
            unit_revision_id=row[11],
        )


def worksheet_exclusion(connection, unit_revision_id):
    """Only a committed exact-target exclusion makes a worksheet noncontributing."""
    row = connection.execute(
        "SELECT c.kind, r.publication_id FROM ukb_revision_source_changes c "
        "JOIN ukb_publication_receipts r ON r.revision_id = c.revision_id "
        "WHERE c.unit_revision_id = ? LIMIT 1",
        (unit_revision_id,),
    ).fetchone()
    return row
