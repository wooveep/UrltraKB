"""Execution bookkeeping only; recovered files become independent ordinary Sources."""

from typing import Literal

from pydantic import Field, field_validator

from openkb.source_records import Digest, FrozenArtifact, PendingJob, Record, RecordId


class ExecutionBudget(Record):
    max_depth: int = Field(default=8, ge=0, le=100)
    max_sources: int = Field(default=100, ge=1, le=100000)
    max_object_bytes: int = Field(default=32 * 1024**2, ge=1)
    max_total_bytes: int = Field(default=256 * 1024**2, ge=1)
    max_decompressed_bytes: int = Field(default=512 * 1024**2, ge=1)
    max_discovery_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)


class ExecutionGroup(Record):
    root_import_id: RecordId
    kb_generation: str
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    budget_origin: str = "default"
    sources: int = Field(default=1, ge=1)
    object_bytes: int = Field(default=0, ge=0)
    decompressed_bytes: int = Field(default=0, ge=0)
    discovery_seconds: float = Field(default=0.0, ge=0)
    cancelled: bool = False


class ImportIntent(PendingJob):
    intent_id: RecordId
    payload: FrozenArtifact
    digest: Digest
    filename: str
    source_id: RecordId | None = None
    source_revision_id: RecordId | None = None
    result: "PendingImportResult | None" = None


class PendingImportResult(Record):
    attempt_id: RecordId
    source_id: RecordId
    source_revision_id: RecordId
    quality_known: bool
    quality: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    units: tuple[dict, ...] = ()
    model_usage: dict | None = None

    @field_validator("model_usage")
    @classmethod
    def check_usage(cls, value):
        from openkb.llm_usage_models import validate_receipt

        return validate_receipt(value)


class DiscoveryCheckpoint(Record):
    checkpoint_id: RecordId
    discovery_intent_id: RecordId
    object_key: str
    policy: str
    payload: FrozenArtifact | None = None
    digest: Digest | None = None
    import_intent_id: RecordId | None = None
    diagnostic: str | None = None
    outcome: Literal[
        "unknown",
        "recovered",
        "private_object",
        "preview",
        "external_reference",
        "corrupt_object",
        "requires_container_rebuild",
        "cycle",
    ] = "unknown"


class JobAttempt(Record):
    attempt_id: RecordId
    intent_id: RecordId
    kind: str
    status: str
    message: str | None = None
    result: PendingImportResult | None = None


class DerivedExecution(Record):
    root_import_id: RecordId
    kb_generation: str
    depth: int = Field(ge=1)
    ancestry: tuple[Digest, ...]
    discovery_policy: str
