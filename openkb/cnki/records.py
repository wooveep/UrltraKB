"""Conversion provenance remains distinct from Office and source identity."""

from typing import Literal

from pydantic import Field

from openkb.source_records import Digest, Record


class CNKIConversion(Record):
    input_digest: Digest
    internal_format: Literal["CAJ", "KDH", "PDF"]
    processing_identity: dict
    pdf_digest: Digest
    pages: int = Field(ge=1)
    declared_pages: int | None = Field(default=None, ge=1)
    diagnostics: list[str] = Field(default_factory=list)
