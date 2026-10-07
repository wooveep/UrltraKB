"""Validated Office distribution and conversion identities, independent of UNO."""

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from openkb.source_records import Digest, Record, RelativePath


class OfficeArtifact(Record):
    url: str
    sha256: Digest
    bytes: int


class OfficeManifest(Record):
    version: Literal["26.2.6.3"] = "26.2.6.3"
    build_id: Literal["8221e31b3ac356a1623c672912a3d2b492f7e3d1"]
    platform: Literal["linux", "win32", "darwin"]
    architecture: Literal["x86_64", "arm64"] = "x86_64"
    archive: OfficeArtifact
    source: OfficeArtifact
    python_version: Literal["3.12.14"]
    python: RelativePath
    soffice: RelativePath
    launcher: RelativePath | None = None
    probe: dict[str, str]
    files: dict[RelativePath, Digest]
    links: dict[RelativePath, RelativePath]
    fonts: dict[RelativePath, Digest]
    licenses: dict[RelativePath, Digest]
    application_fonts_manifest: Digest

    @model_validator(mode="after")
    def validate_inventory(self):
        if (self.platform == "darwin") != (self.architecture == "arm64"):
            raise ValueError("Office platform and architecture do not match a supported runtime")
        if any(
            path and path not in self.files for path in (self.python, self.soffice, self.launcher)
        ):
            raise ValueError("Office executable is absent from its runtime inventory")
        for entries in (self.fonts, self.licenses):
            if not entries or any(self.files.get(path) != value for path, value in entries.items()):
                raise ValueError("Office font/license inventory does not match its runtime files")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()


class RequestedFont(Record):
    text: str
    requested: str
    requested_asian: str
    requested_complex: str


class Slide(Record):
    ordinal: int = Field(ge=1)
    page: int = Field(ge=1)
    name: str
    hidden: bool
    notes: str
    body: str | None = None


class WorkerResult(Record):
    filter: Literal["writer_pdf_Export", "impress_pdf_Export"]
    detected_filter: str
    requested_fonts: list[RequestedFont]
    print_hidden_text: Literal[False] | None = None
    diagnostics: list[str]
    slides: list[Slide] = Field(default_factory=list)


class FontSubstitution(Record):
    text: str
    requested: str
    actual: str


class FontObservation(Record):
    requested: str
    requested_asian: str
    requested_complex: str
    status: Literal["matched", "unmatched", "ambiguous"]
    characters: int = Field(ge=0)
    actual: list[str]


class OfficeConversion(Record):
    input_digest: Digest
    version: Literal["26.2.6.3"]
    build_id: Literal["8221e31b3ac356a1623c672912a3d2b492f7e3d1"]
    python_version: Literal["3.12.14"]
    runtime_fingerprint: Digest
    processing_identity: dict
    probe: dict[str, str]
    archive: OfficeArtifact
    source_archive: OfficeArtifact
    fonts: dict[RelativePath, Digest]
    licenses: dict[RelativePath, Digest]
    elapsed_seconds: float = Field(ge=0)
    filter: Literal["writer_pdf_Export", "impress_pdf_Export"]
    detected_filter: str
    print_hidden_text: Literal[False] | None = None
    diagnostics: list[str]
    pages: int = Field(ge=1)
    pdf_digest: Digest
    pdf_fonts: list[str]
    font_substitutions: list[FontSubstitution]
    font_observations: list[FontObservation]
    slides: list[Slide] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_slide_map(self):
        if self.filter == "impress_pdf_Export":
            if [slide.page for slide in self.slides] != list(range(1, self.pages + 1)) or any(
                slide.ordinal != slide.page or slide.body is None for slide in self.slides
            ):
                raise ValueError("Office slides do not cover the verified physical PDF pages")
        elif self.slides:
            raise ValueError("Writer output cannot contain a slide map")
        return self
