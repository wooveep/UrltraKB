"""One generator result schema for JSON, native task receipts and HTTP events."""

from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ultrakb.llm_usage_models import validate_receipt
from ultrakb.source_records import RecordId

if TYPE_CHECKING:
    from .generators import GenerationResult


class GenerationValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class GenerationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    status: Literal["completed", "failed", "blocked", "conflict", "invalid"]
    output_dir: str | None = None
    archive_path: str | None = None
    artifact_path: str | None = None
    validation: GenerationValidation | None = None
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    quality: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    message: str | None = None
    error_type: str | None = None
    generation_id: RecordId | None = None
    artifact_epoch: int | None = Field(default=None, ge=0, strict=True)
    projection_pending: StrictBool = False
    publication_id: RecordId | None = None
    candidate_revision_id: RecordId | None = None
    execution_id: RecordId | None = None
    model_usage: dict | None = None

    @model_validator(mode="after")
    def valid_usage(self):
        validate_receipt(self.model_usage)
        return self


def generation_receipt(result: "GenerationResult") -> dict:
    values = asdict(result)
    for key in ("output_dir", "archive_path", "artifact_path"):
        if values[key] is not None:
            values[key] = str(values[key])
    values["validation"] = (
        {"errors": result.validation.errors, "warnings": result.validation.warnings}
        if result.validation is not None
        else None
    )
    return GenerationReceipt(**values).model_dump(mode="json")


def with_generation_usage(scope, context, result, previous):
    from .result_usage import confirm_result_usage

    execution = context.storage_execution
    identity = (
        execution.execution_id
        if execution is not None and (execution is not previous or result.generation_id is not None)
        else previous.execution_id
        if previous is not None
        else context.resume_storage_execution_id
        if result.generation_id is not None
        else None
    )
    if identity is None:
        return result
    result = replace(result, execution_id=identity)
    return confirm_result_usage(
        result, context, scope.kb_dir, scope.knowledge.identity, execution_id=identity
    )
