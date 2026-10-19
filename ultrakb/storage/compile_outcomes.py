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
