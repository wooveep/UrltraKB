"""Visual connection, capability declaration and finite per-task limits."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

IMAGE_API_KEY_ENV = "OPENKB_IMAGE_API_KEY"
IMAGE_CONNECTION_ENV = "OPENKB_IMAGE_CONNECTION"
DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}


class VisionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    seconds: float = Field(default=120, gt=0, le=3600, allow_inf_nan=False)
    request_seconds: float = Field(default=45, gt=0, le=300, allow_inf_nan=False)
    max_requests: int = Field(default=4, gt=0, le=100)
    max_tokens: int = Field(default=8192, gt=0, le=1_000_000)
    output_tokens: int = Field(default=2048, gt=0, le=32768)
    image_bytes: int = Field(default=8_000_000, gt=0, le=20_000_000)
    response_bytes: int = Field(default=256_000, gt=0, le=4_000_000)


class VisionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    enabled: bool = False
    connection: Literal["independent", "reuse_main"] = "independent"
    provider: Literal["openai", "openai-compatible", "anthropic", "ollama"] = "openai"
    model: str = ""
    endpoint: str | None = None
    authentication: Literal["api_key", "none"] = "api_key"
    supports_images: bool = False
    limits: VisionLimits = Field(default_factory=VisionLimits)

    @model_validator(mode="after")
    def validate_connection(self):
        if self.endpoint is not None:
            url = urlsplit(self.endpoint)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("Image endpoint must be an HTTP(S) URL without credentials")
        if len(self.model) > 200 or any(ord(char) < 32 for char in self.model):
            raise ValueError("Invalid image model identifier")
        return self

    def identity(self) -> dict:
        return {
            "connection": self.connection,
            "provider": self.provider,
            "model": self.model,
            "endpoint": (self.endpoint or DEFAULT_ENDPOINTS.get(self.provider, "")).rstrip("/"),
            "authentication": self.authentication,
        }


def vision_settings(value) -> VisionSettings:
    try:
        return VisionSettings.model_validate(value if value is not None else {})
    except ValueError:
        raise ValueError("Invalid image-understanding settings") from None
