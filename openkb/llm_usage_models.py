"""Validate saved/public usage receipts at serialization boundaries."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openkb.source_records import RecordId as Identity

Count = Annotated[int, Field(ge=0)]


class _UsageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UsageExecution(_UsageModel):
    schema_version: Literal[1] = 1
    execution_id: Identity
    operation: str = Field(min_length=1)
    state: Literal["started", "completed", "interrupted"]
    task_id: Identity | None = None
    pending_intent_id: Identity | None = None
    pending_attempt_id: Identity | None = None
    source_ids: list[Identity] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self):
        if (self.pending_intent_id is None) != (self.pending_attempt_id is None):
            raise ValueError("Pending execution requires intent and attempt identities")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Execution source identities must be unique")
        return self


class SourceUsageHistory(_UsageModel):
    history_status: Literal["recorded", "partially_recorded", "unrecorded"]


class StageUsage(_UsageModel):
    requests: Count
    input_total: Count
    output_total: Count


class UsageAggregate(_UsageModel):
    input_total: Count
    output_total: Count
    cached_input: Count
    reasoning_output: Count
    total_known: Count
    requests: Count
    unknown_requests: Count
    in_flight_requests: Count
    unknown_fields: dict[
        Literal["input_total", "output_total", "cached_input", "reasoning_output"], Count
    ]
    collection_complete: bool
    usage_complete: bool
    request_ids: list[Identity]
    stages: dict[str, StageUsage]

    @model_validator(mode="after")
    def consistent(self):
        if self.requests != len(set(self.request_ids)) or self.requests != len(self.request_ids):
            raise ValueError("Usage request set does not match its count")
        if self.total_known != self.input_total + self.output_total:
            raise ValueError("Usage total must be the sum of known input and output")
        if self.unknown_requests > self.requests or self.in_flight_requests > self.unknown_requests:
            raise ValueError("Invalid unknown usage counts")
        if set(self.unknown_fields) != {
            "input_total",
            "output_total",
            "cached_input",
            "reasoning_output",
        }:
            raise ValueError("Invalid usage field coverage")
        if any(value > self.requests for value in self.unknown_fields.values()):
            raise ValueError("Invalid usage field counts")
        return self


class UsageReceipt(_UsageModel):
    execution_ids: list[Identity]
    source_ids: list[Identity]
    ledger: str
    current: UsageAggregate
    cumulative: UsageAggregate
    history_status: Literal["recorded", "unrecorded", "partially_recorded", "execution_only"]


def validate_receipt(value: dict | None) -> dict | None:
    if value is not None:
        UsageReceipt.model_validate(value)
    return value
