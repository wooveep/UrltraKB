"""Applicability, document lineage, and immutable annotation identities."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from openkb.source_records import Record, RecordId, ViewId


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Metadata labels cannot be blank")
    return value.strip()


Label = Annotated[str, AfterValidator(_nonblank)]


class SourceMetadata(Record):
    product: Label | None = None
    product_id: RecordId | None = Field(default=None, exclude_if=lambda value: value is None)
    applicable_versions: tuple[Label, ...] = ()
    family: Label | None = None
    document_revision: Label | None = None

    @model_validator(mode="after")
    def unique_versions(self) -> "SourceMetadata":
        if len(set(self.applicable_versions)) != len(self.applicable_versions):
            raise ValueError("Applicable versions must not contain duplicates")
        return self


class Product(Record):
    product_id: RecordId
    name: Label
    aliases: tuple[Label, ...] = Field(default=(), exclude_if=lambda value: not value)
    retired_into: RecordId | None = Field(default=None, exclude_if=lambda value: value is None)


class DocumentFamily(Record):
    family_id: RecordId
    product_id: RecordId | None
    purpose: Label


class FamilyDefault(Record):
    family_id: RecordId
    view_id: ViewId | None


class KnowledgeView(Record):
    view_id: ViewId
    product_id: RecordId | None = None
    product: Label | None = None
    applicable_versions: tuple[Label, ...] = ()
    unknown_source_id: RecordId | None = None

    @model_validator(mode="after")
    def explicit_identity(self) -> "KnowledgeView":
        if self.view_id != "legacy" and self.unknown_source_id is None:
            if not self.product_id or not self.product or not self.applicable_versions:
                raise ValueError(
                    "A shared view requires a confirmed product and complete applicability"
                )
        return self


class VersionCandidate(Record):
    field: Literal["product", "applicable_versions", "family", "document_revision"]
    values: tuple[Label, ...] = Field(min_length=1)
    location: Label
    excerpt: Label
    confidence: Literal["verified", "hint"] = "verified"
    policy: Label = "legacy-title-v1"


class VersionAnnotation(Record):
    annotation_id: RecordId
    source_id: RecordId
    source_revision_id: RecordId
    previous_annotation_id: RecordId | None = None
    family_id: RecordId | None = None
    view_id: ViewId
    metadata: SourceMetadata
    evidence: dict[str, str] = Field(default_factory=dict)
    candidates: tuple[VersionCandidate, ...] = ()
