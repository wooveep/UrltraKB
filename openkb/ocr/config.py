"""Strict OCR settings: selected backend, separate credentials and finite limits."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasGenerator, BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from openkb.ocr.credentials import OCR_API_KEY_ENV


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
    model_config = ConfigDict(alias_generator=AliasGenerator(serialization_alias=to_camel))

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
    # Existing custom references still load; editors accept a separate API key.
    credential_env: str = OCR_API_KEY_ENV
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
            "options": self.options.model_dump(by_alias=True),
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
    runtime: Literal["native", "openvino"] = "native"
    gpu_device: str | None = Field(default=None, pattern=r"^(GPU\.[0-9]+|gpu:[0-9]+)$")
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
            "backend": "paddleocr-vl15-openvino-v1"
            if self.runtime == "openvino"
            else "paddleocr-vl16-native-v3",
            "runtime": self.runtime,
            "gpu_device": self.gpu_device,
            "worker": HashRegistry.hash_file(
                Path(__file__).with_name(
                    "openvino_worker.py" if self.runtime == "openvino" else "worker.py"
                )
            ),
            "loading": HashRegistry.hash_file(Path(__file__).with_name("loading.py")),
            "assets": self.assets_sha256,
            "parameters": self.parameters.model_dump(),
            "python": "3.12.13",
            **(
                {"openvino": "2025.4.1", "precision": "upstream-fp16"}
                if self.runtime == "openvino"
                else {"paddleocr": "3.7.0", "paddlex": "3.7.2", "paddlepaddle": "3.3.1"}
            ),
        }


class ServiceSettings(Settings):
    endpoint: str
    protocol: Literal["pipeline", "vlm"] = "pipeline"
    model: Literal["PaddleOCR-VL-1.6"] = "PaddleOCR-VL-1.6"
    credential_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z_0-9]*$")
    seconds: float = Field(default=120.0, gt=0, le=3600, allow_inf_nan=False)
    max_pages: int = Field(default=100, gt=0, le=10000)
    output_bytes: int = Field(default=32_000_000, gt=0, le=64_000_000)
    output_tokens: int = Field(default=2048, gt=0, le=16384)

    @model_validator(mode="after")
    def endpoint_valid(self):
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("OCR service requires an explicit HTTP(S) endpoint")
        return self

    def profile(self):
        return {
            "backend": "paddleocr-service-v1",
            **self.model_dump(exclude={"credential_env"}),
            "processing_location": "this_machine"
            if urlsplit(self.endpoint).hostname in {"127.0.0.1", "::1", "localhost"}
            else "remote_service",
        }


class OcrSettings(Settings):
    policy: Literal["auto", "off"] = "auto"
    backend: Literal["system", "local", "cloud"] = "system"
    device: Literal["auto", "cpu", "gpu"] = "auto"
    gpu_device: str | None = Field(default=None, pattern=r"^(GPU\.[0-9]+|gpu:[0-9]+)$")
    installation: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution: Literal["runtime", "service"] = "runtime"
    service: ServiceSettings | None = None
    local: LocalSettings | None = None
    cloud: CloudSettings | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_local_default(cls, value):
        if (
            isinstance(value, dict)
            and value.get("backend", "local") == "local"
            and "policy" not in value
            and not {"device", "installation", "execution", "service", "gpu_device"} & value.keys()
        ):
            value = dict(value)
            if value.get("local") is None:
                # Previously "local" without a runtime meant OS-provided OCR.
                value["backend"] = "system"
            else:
                value["backend"] = "local"
                value["device"] = "cpu"
        return value

    def profile(self) -> dict:
        if self.policy == "off":
            return {"policy": "off"}
        if self.backend == "local" and self.execution == "service":
            return {
                "policy": self.policy,
                **(
                    self.service.profile()
                    if self.service
                    else {"backend": "service", "configured": False}
                ),
            }
        selected = self.local if self.backend == "local" else self.cloud
        if self.backend == "system":
            selected = None
        return {
            "policy": self.policy,
            "device": self.device if self.backend == "local" else "system-managed",
            "installation": self.installation if self.backend == "local" else None,
            **(selected.profile() if selected else {"backend": self.backend, "configured": False}),
            "gpu_device": self.gpu_device or (self.local.gpu_device if self.local else None),
        }


class ParsingSettings(Settings):
    ocr: OcrSettings = Field(default_factory=OcrSettings)


def parsing_settings(value) -> ParsingSettings:
    try:
        return ParsingSettings.model_validate(value if value is not None else {})
    except (ValidationError, ValueError, TypeError):
        # Pydantic's ordinary error includes rejected input values; configuration
        # errors must not copy accidental credentials into task diagnostics.
        raise ValueError("Invalid parsing settings; check fields and positive OCR limits") from None
