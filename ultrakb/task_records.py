"""Versioned task query payloads shared by CLI, HTTP and desktop presentation."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ultrakb.llm_usage import UsageRequest
from ultrakb.llm_usage_models import UsageAggregate
from ultrakb.source_records import RecordId
from ultrakb.stage_observations import StageObservation


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TaskBudget(TaskRecord):
    model_budget_id: RecordId
    used: int = Field(ge=0)
    max_calls: int | None = Field(ge=0)
    remaining: int | None = Field(ge=0)
    deadline_at: float | None


class TaskOverview(TaskRecord):
    task_id: RecordId
    operation: str
    state: Literal["started", "stopping", "completed", "failed", "cancelled", "interrupted"]
    created_at: str
    latest_execution_id: RecordId
    started_at: str
    ended_at: str | None
    duration_seconds: float | None = Field(ge=0)
    fingerprint: str
    budget: TaskBudget
    usage: UsageAggregate | None


class TaskExecution(TaskRecord):
    execution_id: RecordId
    generation: str
    state: str
    started_at: str
    ended_at: str | None
    duration_seconds: float | None = Field(ge=0)
    fingerprint: str
    application_task_id: RecordId | None = None
    application_unit_id: Annotated[str, Field(pattern=r"^[0-9]{1,5}$")] | None = None
    parent_application_task_id: RecordId | None = None


class TaskInput(TaskRecord):
    source_id: RecordId
    source_revision_id: RecordId
    source_ordinal: int = Field(ge=0)
    name: str
    original_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    original_bytes: int = Field(ge=0)
    unit_revision_id: RecordId | None = None
    unit_id: RecordId | None = None
    unit_key: str | None = None
    unit_label: str | None = None
    document_name: str | None = None
    processing_fingerprint: str | None = None


class TaskStage(TaskRecord):
    stage_attempt_id: RecordId
    parent_stage_id: RecordId | None
    stage_code: str
    work_key: str | None
    ordinal: int | None = Field(ge=0)
    source_revision_id: RecordId | None
    unit_revision_id: RecordId | None
    state: str
    started_at: str
    ended_at: str | None
    duration_seconds: float | None = Field(ge=0)
    error_code: str | None
    input: TaskInput | None = None
    input_unavailable_reason: str | None = None
    observation: StageObservation | None = None
    observation_unavailable_reason: str | None = None
    observation_coverage: Literal["not_recorded", "pending", "recorded", "partial", "retired"] = (
        "not_recorded"
    )


class TaskList(TaskRecord):
    schema_version: Literal[1] = 1
    statistics_at: str
    kb_id: str
    generation: str
    tasks: tuple[TaskOverview, ...]
    next_cursor: int | None


class TaskPublication(TaskRecord):
    publication_id: RecordId
    knowledge_revision_id: RecordId
    execution_id: RecordId
    generation: str
    attempt_id: RecordId | None
    published_at: str
    view_id: str
    source_revision_id: RecordId | None
    unit_revision_id: RecordId | None
    projection_status: Literal[
        "pending", "paused", "conflict", "synced", "superseded", "not_recorded"
    ]
    projection_details: Literal["recorded", "retired"] = "recorded"


class TaskDetail(TaskRecord):
    schema_version: Literal[1] = 1
    statistics_at: str
    kb_id: str
    generation: str
    task: TaskOverview
    execution_id: RecordId
    execution: TaskExecution
    execution_usage: UsageAggregate | None
    executions: tuple[TaskExecution, ...]
    stages: tuple[TaskStage, ...]
    requests: tuple[UsageRequest, ...]
    publications: tuple[TaskPublication, ...]
    executions_after: int | None
    stages_after: int | None
    requests_after: int | None
    publications_after: int | None
    coverage: dict[str, str | bool]


class TaskStageDetail(TaskRecord):
    schema_version: Literal[1] = 1
    statistics_at: str
    kb_id: str
    generation: str
    task_id: RecordId
    execution_id: RecordId
    stage: TaskStage
    usage_scope: Literal["source", "unit"] | None
    current_usage: UsageAggregate | None
    cumulative_usage: UsageAggregate | None
    requests: tuple[UsageRequest, ...]
    requests_after: int | None
    coverage: dict[str, str | bool]
