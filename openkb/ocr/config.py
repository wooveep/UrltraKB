"""Strict OCR settings: selected backend, separate credentials and finite limits."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CloudLimits(Settings):
    seconds: float = Field(gt=0, allow_inf_nan=False)
    request_seconds: float = Field(gt=0, allow_inf_nan=False)
    poll_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_requests: int = Field(gt=0)
    max_pages: int = Field(gt=0)
    max_page_bytes: int = Field(gt=0, le=48_000_000)
    max_download_bytes: int = Field(gt=0)


class CloudOptions(Settings):
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_layout_detection: Literal[True] = True
    use_ocr_for_image_block: Literal[True] = True
    merge_tables: Literal[False] = False
    restructure_pages: Literal[False] = False
    return_markdown_images: Literal[True] = True
    markdown_ignore_labels: list[str] = Field(default_factory=list, max_length=0)


class CloudSettings(Settings):
    endpoint: str
    model: str
    credential_env: str
    limits: CloudLimits
    options: CloudOptions = Field(default_factory=CloudOptions)

    @model_validator(mode="after")
    def fields_valid(self):
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("OCR endpoint must be an HTTP(S) jobs URL without credentials")
        if not self.model.strip() or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", self.credential_env):
            raise ValueError("Invalid OCR model or credential reference")
        return self

    def profile(self) -> dict:
        return {
            "backend": "paddleocr-jobs-single-page-v2",
            "endpoint": self.endpoint,
            "model": self.model,
            "options": self.options.model_dump(),
        }


class LocalLimits(Settings):
    seconds: float = Field(gt=0, allow_inf_nan=False)
    cleanup_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_pages: int = Field(gt=0)
    memory_bytes: int = Field(gt=0)
    output_bytes: int = Field(gt=0)
    max_regions: int = Field(gt=0)
    max_tokens: int = Field(gt=0)


class LocalParameters(Settings):
    max_new_tokens: int = Field(gt=0)
    min_pixels: int = Field(gt=0)
    max_pixels: int = Field(gt=0)
    render_dpi: int = Field(gt=0)
    threads: int = Field(gt=0)

    @model_validator(mode="after")
    def pixel_bounds(self):
        if self.min_pixels > self.max_pixels:
            raise ValueError("Minimum recognition pixels exceed maximum pixels")
        return self


class LocalSettings(Settings):
    interpreter: str
    assets: str
    assets_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    limits: LocalLimits
    parameters: LocalParameters

    @model_validator(mode="after")
    def absolute_paths(self):
        from pathlib import Path

        if not all(Path(value).is_absolute() for value in (self.interpreter, self.assets)):
            raise ValueError("OCR interpreter and model assets require absolute paths")
        return self

    def profile(self) -> dict:
        from pathlib import Path

        from openkb.state import HashRegistry

        return {
            "backend": "paddleocr-vl16-cpu-v2",
            "worker": HashRegistry.hash_file(Path(__file__).with_name("worker.py")),
            "loading": HashRegistry.hash_file(Path(__file__).with_name("loading.py")),
            "assets": self.assets_sha256,
            "parameters": self.parameters.model_dump(),
            "paddleocr": "3.7.0",
            "paddlex": "3.7.2",
            "paddlepaddle": "3.3.1",
            "python": "3.12.13",
        }


class OcrSettings(Settings):
    backend: Literal["local", "cloud"] = "local"
    local: LocalSettings | None = None
    cloud: CloudSettings | None = None

    def profile(self) -> dict:
        selected = self.local if self.backend == "local" else self.cloud
        return selected.profile() if selected else {"backend": self.backend, "configured": False}


class ParsingSettings(Settings):
    ocr: OcrSettings = Field(default_factory=OcrSettings)


def parsing_settings(value) -> ParsingSettings:
    try:
        return ParsingSettings.model_validate(value if value is not None else {})
    except (ValidationError, ValueError, TypeError):
        # Pydantic's ordinary error includes rejected input values; configuration
        # errors must not copy accidental credentials into task diagnostics.
        raise ValueError("Invalid parsing settings; check fields and positive OCR limits") from None
