"""Small validated compiler outcomes bound to durable work and publication claims."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Label = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class CompileOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    quality_known: bool = False
    quality: tuple[Label, ...] = Field(default=(), max_length=256)
    unfinished: tuple[Label, ...] = Field(default=(), max_length=256)


def read_outcome(connection, revision_id):
    row = connection.execute(
        "SELECT outcome_json FROM ukb_revision_import WHERE revision_id = ? "
        "UNION ALL SELECT outcome_json FROM ukb_revision_refresh WHERE revision_id = ?",
        (revision_id, revision_id),
    ).fetchone()
    return CompileOutcome.model_validate_json(row[0]) if row else CompileOutcome()


def read_unit_outcome(root, identity, unit_revision_id, publication_id, view_id):
    """Prefer current failed work, then the unit's committed outcome in this snapshot."""
    from .coordination import storage_stage

    with storage_stage(root, identity, read_only=True) as db:
        row = db.connection.execute(
            "SELECT r.record_json FROM ukb_import_intents i "
            "JOIN ukb_pending_attempts a ON a.attempt_id = i.attempt_id "
            "JOIN ukb_executions e ON e.execution_id = a.execution_id "
            "JOIN ukb_compile_work w ON w.model_budget_id = e.model_budget_id "
            "JOIN ukb_compile_records r ON r.work_id = w.work_id "
            "AND r.step_key = 'compile-outcome' "
            "WHERE i.unit_revision_id = ? AND i.kind = 'import' "
            "AND json_extract(w.object_json, '$.unit_revision_id') = i.unit_revision_id "
            "AND json_extract(w.object_json, '$.publication_id') = ? "
            "AND json_extract(w.object_json, '$.view_id') = ? "
            "ORDER BY i.sequence DESC LIMIT 1",
            (unit_revision_id, publication_id, view_id),
        ).fetchone()
        if row is None:
            row = db.connection.execute(
                "SELECT accepted.outcome_json FROM ukb_import_intents i "
                "JOIN ukb_pending_attempts a ON a.intent_id = i.intent_id "
                "JOIN ukb_publication_receipts receipt ON receipt.attempt_id = a.attempt_id "
                "JOIN ukb_revision_import accepted ON accepted.revision_id = receipt.revision_id "
                "JOIN ukb_publications p ON p.publication_id = receipt.publication_id "
                "WHERE i.unit_revision_id = ? AND i.kind = 'import' AND p.view_id = ? "
                "AND p.rowid <= (SELECT rowid FROM ukb_publications WHERE publication_id = ?) "
                "ORDER BY p.rowid DESC LIMIT 1",
                (unit_revision_id, view_id, publication_id),
            ).fetchone()
    return CompileOutcome.model_validate_json(row[0]) if row else CompileOutcome()
