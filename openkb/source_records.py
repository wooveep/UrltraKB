"""Validated, versioned records for admitted documents and their frozen inputs."""

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

RecordId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ViewId = Annotated[str, Field(pattern=r"^(legacy|[a-f0-9]{32})$")]
DocName = Annotated[str, Field(pattern=r"^[^./\\][^/\\]*$")]


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError("Expected a contained relative path")
    return value


RelativePath = Annotated[str, AfterValidator(_relative_path)]
FrozenArtifact = Annotated[
    str, Field(pattern=r"^\.openkb/artifacts/[a-f0-9]{64}/content(?:\.[a-z0-9]+)?$")
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1


ENCODING_POLICY = "bom-utf8-charset-normalizer-3.4.7-v1"


class EncodingDecision(Record):
    name: str
    basis: Literal["bom", "utf8", "detected", "signature", "declaration"]
    policy: str = ENCODING_POLICY
    chaos: float | None = Field(default=None, ge=0, le=1)
    coherence: float | None = Field(default=None, ge=0, le=1)


class TextDecoding(Record):
    encoding: EncodingDecision | None = None
    diagnostics: tuple[str, ...] = ()
    error: str | None = None
    policy: str = ENCODING_POLICY


class Source(Record):
    source_id: RecordId
    identity: str
    name: str
    doc_name: DocName
    target_revision_id: RecordId
    target_generation: int = Field(ge=1)
    removed: bool = False
    contribution_empty: bool = False
    excluded_inputs: dict[RecordId, Literal["empty", "withdrawn"]] = Field(default_factory=dict)
    legacy_hash: str | None = None
    annotation_id: RecordId | None = None
    family_id: RecordId | None = None


class FrozenAsset(Record):
    original_reference: str
    artifact: FrozenArtifact | None
    digest: Digest | None

    @model_validator(mode="after")
    def matching_digest(self):
        if (self.artifact is None) != (self.digest is None):
            raise ValueError("Frozen asset requires both artifact and digest")
        if self.artifact and self.artifact.split("/")[2] != self.digest:
            raise ValueError("Frozen asset digest does not match its artifact")
        return self


class SourceRevision(Record):
    text_decoding: TextDecoding | None = Field(default=None, exclude_if=lambda value: value is None)
    source_revision_id: RecordId
    source_id: RecordId
    discovery_intent_id: RecordId
    original: FrozenArtifact
    digest: Digest
    source_format: str
    assets: tuple[FrozenAsset, ...] = ()
    created_at: str
    original_kind: Literal["original", "legacy_snapshot"] = "original"

    @model_validator(mode="after")
    def matching_digest(self):
        if self.original.split("/")[2] != self.digest:
            raise ValueError("Frozen source digest does not match its artifact")
        return self


class DiscoveryIntent(Record):
    intent_id: RecordId
    source_revision_id: RecordId
    original: FrozenArtifact
    root_import_id: RecordId
    kb_generation: str
    status: Literal["pending", "completed"] = "pending"
    cancelled: bool = False
