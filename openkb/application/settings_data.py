"""Validated settings values shared by the application adapters.

A patch retains omitted fields separately from explicit nulls. Credentials
use SecretStr to keep accidental representations from exposing their values.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr

from openkb.compilation_settings import CompilationSettings, Effort, ReviewMode, Thinking
from openkb.ocr.config import ParsingSettings
from openkb.vision.config import VisionSettings


def _processing_settings(value):
    """Check a processing shape before its effective model is available.

    A settings patch is validated before it is merged with the selected model
    and endpoint.  Capacity is therefore checked by the application writer
    after that merge; doing it here would either validate against an invented
    model or reject a valid independent input/output declaration.
    """
    from openkb.processing import RequestLimits

    fields = set(RequestLimits.__dataclass_fields__)
    optional = {
        "context_tokens",
        "max_context_tokens",
        "max_output_tokens",
        "timeout_retries",
        "input_tokens",
        "max_input_tokens",
        "shared_context",
    }
    if not isinstance(value, dict) or not fields - optional <= set(value) <= fields:
        raise ValueError("Provide all processing budget fields and no unknown fields")
    if any(
        isinstance(item, bool)
        for key, item in value.items()
        if key != "shared_context" and item is not None
    ):
        raise ValueError("Processing budget values must not use boolean values")
    integer_fields = {
        "context_tokens",
        "output_tokens",
        "max_context_tokens",
        "max_output_tokens",
        "timeout_retries",
        "input_tokens",
        "max_input_tokens",
        "max_attempts",
        "max_requests",
        "max_tokens",
        "concurrency",
    }
    if (
        any(
            key in value and value[key] is not None and type(value[key]) is not int
            for key in integer_fields
        )
        or any(
            key in value and value[key] is not None and not isinstance(value[key], (int, float))
            for key in ("request_timeout", "stage_timeout", "document_timeout", "cleanup_timeout")
        )
        or ("shared_context" in value and type(value["shared_context"]) is not bool)
    ):
        raise ValueError("Processing budget fields have invalid types")
    return dict(value)


ProcessingSettings = Annotated[dict[str, Any], BeforeValidator(_processing_settings)]


class NavigationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)
    window_tokens: int = Field(default=200000, strict=True, gt=0)
    summaries: bool = Field(default=True, strict=True)
    processing: ProcessingSettings | None = None


class _KbConfigWritable(BaseModel):
    """Typed schema for the writable ``config.yaml`` fields.

    All fields are optional so a partial merge-patch validates. Used to reject
    a wrong-typed config VALUE with a 400 BEFORE it is persisted: a bad value
    that reached disk would make every future ``read_kb_config`` re-read it and
    500 (persisted corruption + endpoint DoS). Types MUST match what
    ``read_kb_config``/``KbConfigResponse`` read back.
    """

    review_mode: ReviewMode | None = None
    compilation_thinking: Thinking | None = None
    planning_thinking: Thinking | None = None
    verification_thinking: Thinking | None = None
    verification_adjudication_thinking: Thinking | None = None
    correction_thinking: Thinking | None = None
    compilation_reasoning_effort: Effort | None = None
    planning_reasoning_effort: Effort | None = None
    verification_reasoning_effort: Effort | None = None
    verification_adjudication_reasoning_effort: Effort | None = None
    correction_reasoning_effort: Effort | None = None
    model: str | None = None
    language: str | None = None
    pageindex_threshold: int | None = None
    # Entity-type vocabulary for extraction. A list REPLACES the layer below; an
    # explicit null reverts to inherited. Values are cleaned/deduped and "other"
    # is always ensured at read time (config.resolve_entity_types).
    entity_types: list[str] | None = None
    parsing: ParsingSettings | None = None
    image_understanding: VisionSettings | None = None
    processing: ProcessingSettings | None = None
    navigation: NavigationSettings | None = None


# Single source of truth for the writable config keys (derived from the model
# above so the two never drift).
_KB_CONFIG_WRITABLE_KEYS = set(_KbConfigWritable.model_fields)


class GlobalConfigValues(_KbConfigWritable):
    """Raw global-layer values (null where global.yaml is silent)."""

    # Unlike effective settings, every field stays nullable so the settings UI
    # can distinguish a silent global layer from an explicit default.


class GlobalConfigResponse(CompilationSettings):
    model: str
    parsing: ParsingSettings = Field(default_factory=ParsingSettings)
    image_understanding: VisionSettings = Field(default_factory=VisionSettings)
    processing: ProcessingSettings | None = None
    navigation: NavigationSettings = Field(default_factory=NavigationSettings)
    language: str
    pageindex_threshold: int
    # Effective global entity-type vocabulary (cleaned; always includes "other").
    entity_types: list[str]
    # Effective KB root that kb_root_dir() would return (env OPENKB_KB_ROOT >
    # global.yaml kb_root > default <config>/kbs). kb_root_env_pinned is True
    # when OPENKB_KB_ROOT is set — a global.yaml kb_root is then ineffective, so
    # the UI can note that editing it won't take effect on this server.
    kb_root: str
    kb_root_env_pinned: bool
    # Global-default credentials read from ~/.config/openkb/.env (the
    # lowest-precedence credential source). openai_api_base is plaintext (a
    # config value, not a secret); has_api_key is a presence flag only — the raw
    # key value is NEVER returned by the API. Mirrors KbConfigResponse.
    openai_api_base: str | None
    has_api_key: bool
    has_ocr_api_key: bool = False
    has_image_api_key: bool = False


class GlobalConfigPatchRequest(BaseModel):
    config: dict[str, Any] | None = None
    # Global-default credentials (mirrors KbConfigPatchRequest). Merge-patch
    # semantics via model_fields_set: an ABSENT field is left unchanged, an
    # explicit null CLEARS it. api_key is a SecretStr so its value never lands
    # in logs/reprs.
    api_key: SecretStr | None = None
    ocr_api_key: SecretStr | None = None
    image_api_key: SecretStr | None = None
    openai_api_base: str | None = None
    # RFC 7386 merge-patch (via model_fields_set): a string SETS global.yaml
    # `kb_root`, an explicit null REMOVES it (revert to the default root), and an
    # absent field leaves it unchanged. NOT a credential — persisted to
    # global.yaml, never the .env. Env OPENKB_KB_ROOT still overrides at runtime.
    kb_root: str | None = None


class KbConfigResponse(CompilationSettings):
    model: str
    parsing: ParsingSettings = Field(default_factory=ParsingSettings)
    image_understanding: VisionSettings = Field(default_factory=VisionSettings)
    processing: ProcessingSettings | None = None
    navigation: NavigationSettings = Field(default_factory=NavigationSettings)
    language: str
    pageindex_threshold: int
    # Effective entity-type vocabulary (cleaned; always includes "other").
    entity_types: list[str]
    openai_api_base: str | None
    has_api_key: bool
    has_ocr_api_key: bool = False
    has_image_api_key: bool = False
    # Additive (non-breaking): which layer supplied each scalar's effective
    # value, and the raw global-layer values for the "继承 · 全局(値)" badge.
    sources: dict[str, Literal["kb", "global", "default"]]
    global_values: GlobalConfigValues


class KbConfigPatchRequest(BaseModel):
    kb: str
    config: dict[str, Any] | None = None
    api_key: SecretStr | None = None
    ocr_api_key: SecretStr | None = None
    image_api_key: SecretStr | None = None
    openai_api_base: str | None = None


class SettingsView(BaseModel):
    """Display values and provenance; no credential plaintext crosses this seam."""

    values: GlobalConfigResponse | KbConfigResponse
    sources: dict[str, str]
