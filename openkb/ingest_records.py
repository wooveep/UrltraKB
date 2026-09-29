"""Persistent targets and publication facts; task receipts are only projections."""

from typing import Annotated, Literal

from pydantic import Field

from openkb.source_records import Digest, DocName, Record, RecordId, RelativePath, ViewId


class ImportUnit(Record):
    unit_id: RecordId
    source_id: RecordId
    key: str = "body"
    doc_name: DocName
    target_revision_id: RecordId
    generation: int = Field(default=1, ge=1)


class UnitRevision(Record):
    unit_revision_id: RecordId
    unit_id: RecordId
    source_revision_id: RecordId
    processing_fingerprint: str
    job_id: RecordId


class UnitPublication(Record):
    publication_id: RecordId
    unit_id: RecordId
    view_id: ViewId = "legacy"
    target_revision_id: RecordId
    attempt_id: RecordId
    job_id: RecordId
    status: Literal[
        "pending",
        "started",
        "completed",
        "failed",
        "awaiting_confirmation",
        "interrupted",
        "stopped",
    ]
    successful_revision_id: RecordId | None = None
    knowledge_revision_id: RecordId | None = None
    proposal_id: RecordId | None = None
    stage: str = "admitted"
    error_type: str | None = None
    message: str | None = None


class KnowledgeHead(Record):
    view_id: ViewId = "legacy"
    generation: int = Field(default=0, ge=0)
    knowledge_revision_id: RecordId | None = None
    inputs: dict[RecordId, RecordId] = Field(default_factory=dict)
    generated_baselines: dict[RelativePath, Digest] = Field(default_factory=dict)


class KnowledgeRevision(Record):
    knowledge_revision_id: RecordId
    view_id: ViewId
    base_revision_id: RecordId | None
    unit_revision_id: RecordId
    input_revisions: tuple[RecordId, ...]
    page_dependencies: dict[RelativePath, tuple[RecordId, ...]]
    generated_baselines: dict[RelativePath, Digest]
    original_references: tuple[RelativePath, ...]
    normalized_source: Annotated[RelativePath, Field(pattern=r"^sources/")]
    source_format: str
    normalized_format: Literal["pdf", "markdown"]
    length_class: Literal["short", "long"]
    execution_mode: Literal["full", "segmented"]
    index_ref: str | None = None


class Proposal(Record):
    proposal_id: RecordId
    unit_revision_id: RecordId
    view_id: ViewId
    expected_source_generation: int
    expected_view_generation: int
    expected_pages: dict[RelativePath, Digest]
    conflicts: tuple[str, ...]
    candidate_manifest: KnowledgeRevision
