"""Stable document classification, distinct from the chosen compilation route."""

from typing import Literal

from pydantic import Field, model_validator

from ultrakb.source_records import Record


class ReprocessingRequired(ValueError):
    """A retry cannot execute today's processor under an older saved policy."""


class PdfLimit(Record):
    short_max_pages: int = Field(ge=0, strict=True)
    source: Literal["kb", "global", "default"]
    key: Literal["pdf_short_max_pages", "pageindex_threshold"] = "pdf_short_max_pages"
    legacy_force_index: bool = False
    legacy_threshold: int | None = None

    @property
    def compatibility_threshold(self) -> int:
        return (
            self.legacy_threshold if self.legacy_threshold is not None else self.short_max_pages + 1
        )


class ProcessingDecision(Record):
    length_class: Literal["short", "long"]
    execution_mode: Literal["full", "segmented"]
    measurement_value: int = Field(ge=0, strict=True)
    measurement_unit: Literal["physical_page", "token"] = "physical_page"
    measurement_fingerprint: str = "pdf-physical-pages-v1"
    pdf_limit: PdfLimit | None = None
    capacity_status: Literal["not_needed", "unknown", "sufficient", "insufficient"] = "unknown"
    capacity_reason: str = "No reliable request capacity has been established"
    capacity_source: str = "unknown"
    capacity_model: str | None = None
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    input_limit: int | None = Field(default=None, ge=0)
    output_reserve_tokens: int | None = Field(default=None, ge=0)


class ModelCapacity(Record):
    max_input_bytes: int | None = Field(default=None, gt=0, strict=True)
    max_request_bytes: int | None = Field(default=None, gt=0, strict=True)
    max_input_tokens: int | None = Field(default=None, gt=0, strict=True)
    context_window_tokens: int | None = Field(default=None, gt=0, strict=True)
    output_reserve_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    measurement: Literal["tokenizer", "utf8_token_upper_bound"] = "tokenizer"
    provenance: str | None = None
    tokenizer_model: str | None = None
    applies_to_model: str | None = None

    @model_validator(mode="after")
    def require_consistent_context(self):
        if self.max_input_bytes is not None:
            if self.max_input_tokens is not None or self.tokenizer_model is not None:
                raise ValueError(
                    "Use max_input_bytes for a byte guard or token limits with a tokenizer"
                )
            if self.context_window_tokens is not None or self.output_reserve_tokens is not None:
                raise ValueError("Byte guards and model token capacity use separate capacity modes")
            return self
        if self.measurement == "utf8_token_upper_bound" and self.tokenizer_model is not None:
            raise ValueError("Choose a tokenizer or the explicit UTF-8 token upper bound")
        if (
            self.max_output_tokens is not None
            and self.output_reserve_tokens is not None
            and self.output_reserve_tokens > self.max_output_tokens
        ):
            raise ValueError("Output reserve exceeds the service output capacity")
        if self.context_window_tokens is None:
            return self
        reserve = self.output_reserve_tokens or 0
        if reserve >= self.context_window_tokens:
            raise ValueError("Output reserve must leave room for input within the context window")
        if (
            self.max_input_tokens is not None
            and self.max_input_tokens > self.context_window_tokens - reserve
        ):
            raise ValueError("Input limit and output reserve exceed the context window")
        return self


def resolve_pdf_limit(global_values: dict, kb_values: dict) -> PdfLimit:
    """Select a layer before interpreting new/legacy keys; never guess explicit intent."""
    for source, values in (("kb", kb_values), ("global", global_values)):
        key = "pdf_short_max_pages" if "pdf_short_max_pages" in values else "pageindex_threshold"
        if key not in values or values[key] is None:
            continue
        value = values[key]
        if type(value) is not int or (key == "pdf_short_max_pages" and value < 0):
            raise ValueError(f"Configuration field '{key}' must be an integer")
        return PdfLimit(
            short_max_pages=value if key == "pdf_short_max_pages" else max(0, value - 1),
            source=source,
            key=key,
            legacy_force_index=key == "pageindex_threshold" and value <= 0,
            legacy_threshold=value if key == "pageindex_threshold" else None,
        )
    return PdfLimit(short_max_pages=10, source="default")


def classify_pdf(page_count: int, config: dict) -> ProcessingDecision:
    limit = PdfLimit.model_validate(config["pdf_limit"])
    length = "long" if limit.legacy_force_index or page_count > limit.short_max_pages else "short"
    return ProcessingDecision(
        length_class=length,
        execution_mode="segmented" if length == "long" else "full",
        measurement_value=page_count,
        pdf_limit=limit,
        capacity_status="not_needed" if length == "long" else "unknown",
        capacity_reason="Long document uses the shared index"
        if length == "long"
        else ("No reliable request capacity has been established"),
    )


def classify_markdown_tokens(token_count: int) -> ProcessingDecision:
    """The inclusive 5,000-token business threshold never depends on an LLM model."""
    from ultrakb.text_measurement import MEASUREMENT_FINGERPRINT

    length = "short" if token_count <= 5000 else "long"
    return ProcessingDecision(
        length_class=length,
        execution_mode="full" if length == "short" else "segmented",
        measurement_value=token_count,
        measurement_unit="token",
        measurement_fingerprint=MEASUREMENT_FINGERPRINT,
        capacity_status="not_needed" if length == "long" else "unknown",
        capacity_reason="Long document uses the shared index"
        if length == "long"
        else "No reliable request capacity has been established",
    )
