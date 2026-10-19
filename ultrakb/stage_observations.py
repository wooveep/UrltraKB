"""Bounded observations, distinct from core task and paid-result records."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ultrakb.source_records import RecordId


class ObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class InputObservation(ObservationRecord):
    temporary_input_id: RecordId | None = None
    input_name: Annotated[str, Field(max_length=1024)] | None = None
    input_bytes: int | None = Field(default=None, ge=0)
    input_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    unit_label: Annotated[str, Field(max_length=1024)] | None = None
    item_count: int | None = Field(default=None, ge=0)
    item_kind: Literal["pages", "cells", "tokens", "items"] | None = None

    @model_validator(mode="after")
    def require_item_unit(self):
        if (self.item_count is None) != (self.item_kind is None):
            raise ValueError("An observed item count requires its measurement unit")
        return self


class StorageCosts(ObservationRecord):
    method: Literal["instrumented_payload_io_and_writer_boundaries_v1"] = (
        "instrumented_payload_io_and_writer_boundaries_v1"
    )
    accounting: Literal["exclusive_observed_scope"] = "exclusive_observed_scope"
    writer_attempts: int = Field(default=0, ge=0)
    writer_acquired: int = Field(default=0, ge=0)
    writer_commits: int = Field(default=0, ge=0)
    writer_rollbacks: int = Field(default=0, ge=0)
    writer_errors: int = Field(default=0, ge=0)
    writer_wait_seconds: float = Field(default=0.0, ge=0)
    writer_hold_seconds: float = Field(default=0.0, ge=0)
    writer_max_wait_seconds: float = Field(default=0.0, ge=0)
    writer_max_hold_seconds: float = Field(default=0.0, ge=0)
    blob_read_attempts: int = Field(default=0, ge=0)
    blob_read_bytes: int = Field(default=0, ge=0)
    blob_read_errors: int = Field(default=0, ge=0)
    blob_read_seconds: float = Field(default=0.0, ge=0)
    blob_write_attempts: int = Field(default=0, ge=0)
    blob_write_bytes: int = Field(default=0, ge=0)
    blob_write_errors: int = Field(default=0, ge=0)
    blob_write_seconds: float = Field(default=0.0, ge=0)
    blob_dedup_hits: int = Field(default=0, ge=0)


class StageObservation(ObservationRecord):
    schema_version: Literal[1] = 1
    input: InputObservation | None = None
    storage: StorageCosts | None = None


def encode_observation(value):
    if value is None:
        return None
    encoded = StageObservation.model_validate(value).model_dump_json()
    if len(encoded.encode()) > 16 * 1024:
        raise ValueError("A stage observation must fit in 16 KiB")
    return encoded
