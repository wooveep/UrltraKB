"""Persistent targets and publication facts; task receipts are only projections."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from openkb.processing_policy import ProcessingDecision
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
    annotation_id: RecordId | None = None


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
        "blocked",
    ]
    successful_revision_id: RecordId | None = None
    knowledge_revision_id: RecordId | None = None
    proposal_id: RecordId | None = None
    stage: str = "admitted"
    error_type: str | None = None
    message: str | None = None


class RefreshReason(Record):
    source_id: RecordId
    source_revision_id: RecordId
    target_revision_id: RecordId
    source_generation: int = Field(ge=1)
    kind: Literal["updated", "withdrawn", "empty"]


class KnowledgeHead(Record):
    view_id: ViewId = "legacy"
    generation: int = Field(default=0, ge=0)
    knowledge_revision_id: RecordId | None = None
    inputs: dict[RecordId, RecordId] = Field(default_factory=dict)
    generated_baselines: dict[RelativePath, Digest] = Field(default_factory=dict)
    needs_refresh: dict[RelativePath, tuple[RefreshReason, ...]] = Field(default_factory=dict)


class SourceMap(Record):
    path: Annotated[RelativePath, Field(pattern=r"^sources/")]
    digest: Digest
    unit_kind: Literal["page"] = "page"
    unit_count: int = Field(ge=1)
    assets: dict[RelativePath, Digest] = Field(default_factory=dict)


class KnowledgeRevision(Record):
    knowledge_revision_id: RecordId
    view_id: ViewId
    base_revision_id: RecordId | None
    change_kind: Literal["compile", "manual", "refresh"] = "compile"
    unit_revision_id: RecordId | None = None
    input_revisions: tuple[RecordId, ...]
    page_dependencies: dict[RelativePath, tuple[RecordId, ...]]
    generated_baselines: dict[RelativePath, Digest]
    original_references: tuple[RelativePath, ...]
    normalized_source: Annotated[RelativePath, Field(pattern=r"^sources/")] | None = None
    source_map: SourceMap | None = None
    source_format: str | None = None
    normalized_format: Literal["pdf", "markdown"] | None = None
    length_class: Literal["short", "long"] | None = None
    execution_mode: Literal["full", "segmented"] | None = None
    index_ref: str | None = None
    processing: ProcessingDecision | None = None

    @model_validator(mode="after")
    def validate_compile_input(self) -> "KnowledgeRevision":
        if self.processing and (
            self.processing.length_class != self.length_class
            or self.processing.execution_mode != self.execution_mode
        ):
            raise ValueError("Processing decision does not match the published input")
        if self.change_kind == "compile":
            required = (
                self.unit_revision_id,
                self.normalized_source,
                self.source_format,
                self.normalized_format,
                self.length_class,
                self.execution_mode,
            )
            if any(value is None for value in required):
                raise ValueError("A compile revision requires complete normalized input metadata")
            if self.execution_mode == "segmented" and not self.index_ref:
                raise ValueError("A segmented compile revision requires an index reference")
        return self


class Proposal(Record):
    proposal_id: RecordId
    unit_revision_id: RecordId
    view_id: ViewId
    expected_source_generation: int
    expected_unit_generation: int
    expected_view_generation: int
    expected_kb_generation: str
    expected_pages: dict[RelativePath, Digest]
    candidate_pages: dict[RelativePath, Digest]
    conflicts: tuple[str, ...]
    candidate_manifest: KnowledgeRevision

    @model_validator(mode="after")
    def validate_candidate(self) -> "Proposal":
        if self.candidate_manifest.change_kind != "compile":
            raise ValueError("A proposal requires a compile candidate")
        return self
