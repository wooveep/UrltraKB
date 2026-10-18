"""Independent durable request ledger; never stores prompts, bodies or credentials."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from ultrakb.llm_usage_models import Identity, SourceUsageHistory
from ultrakb.locks import atomic_record_lock, atomic_write_json

if TYPE_CHECKING:
    from ultrakb.storage.model_usage import StoredModelUsage

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    request_attempt_id: Identity
    logical_call_id: Identity | None = None
    parent_call_id: Identity | None = None
    operation: str | None = None
    prompt_version: str | None = None
    policy_fingerprint: str | None = None
    application_cache_hit: bool = False
    execution_id: Identity
    source_id: Identity | None = None
    source_revision_id: Identity | None = None
    unit_id: Identity | None = None
    unit_revision_id: Identity | None = None
    attempt_id: Identity | None = None
    root_import_id: Identity | None = None
    model: str
    stage: str
    started_at: str
    ended_at: str | None = None
    state: Literal["started", "completed", "failed", "cancelled"] = "started"
    observation: Literal["call", "transport"] = "call"
    send_status: Literal["unknown", "not_sent", "sent"] = "unknown"
    business_state: Literal["completed", "failed", "cancelled"] | None = None
    provider_request_id: str | None = None
    input_total: int | None = Field(default=None, ge=0)
    output_total: int | None = Field(default=None, ge=0)
    cached_input: int | None = Field(default=None, ge=0)
    reasoning_output: int | None = Field(default=None, ge=0)
    usage_status: Literal["known", "partial", "unknown"] = "unknown"


@dataclass(frozen=True)
class UsageScope:
    kb_dir: Path
    execution_id: str
    source_id: str | None = None
    source_revision_id: str | None = None
    unit_id: str | None = None
    unit_revision_id: str | None = None
    attempt_id: str | None = None
    root_import_id: str | None = None
    on_event: Callable[[dict], None] | None = None
    generation: str | None = None
    storage: StoredModelUsage | None = None


_SCOPE: ContextVar[UsageScope | None] = ContextVar("ultrakb_import_usage", default=None)


def active_scope() -> UsageScope | None:
    return _SCOPE.get()


@contextmanager
def usage_scope_lifecycle(scope: UsageScope | None) -> Iterator[None]:
    """Bind a retained callback to its captured directory lifetime.

    Legacy manually constructed scopes have no captured identity. Production
    scopes capture one while import_usage_execution holds its lifecycle lease.
    """
    if scope is not None and scope.storage is not None:
        from ultrakb.lifecycle import read_lifecycle

        with read_lifecycle(scope.kb_dir, database_identity=scope.storage.identity):
            yield
        return
    if scope is None or scope.generation is None:
        yield
        return
    from ultrakb.lifecycle import expected_generation, read_lifecycle

    with expected_generation(scope.kb_dir, scope.generation), read_lifecycle(scope.kb_dir):
        yield


@contextmanager
def usage_context(**fields: Any) -> Iterator[None]:
    scope = _SCOPE.get()
    token = _SCOPE.set(replace(scope, **fields)) if scope else None
    try:
        if scope and scope.storage is None and fields.get("source_id"):
            mark_source(scope.kb_dir, fields["source_id"])
            from ultrakb.llm_usage_execution import bind_source_execution

            bind_source_execution(fields["source_id"])
        yield
    finally:
        if token is not None:
            _SCOPE.reset(token)


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def normalize_usage(value: Any) -> dict:
    input_total = _count(_field(value, "input_tokens"))
    if input_total is None:
        input_total = _count(_field(value, "prompt_tokens"))
    output_total = _count(_field(value, "output_tokens"))
    if output_total is None:
        output_total = _count(_field(value, "completion_tokens"))
    cached = _count(_field(_field(value, "input_tokens_details"), "cached_tokens"))
    if cached is None:
        cached = _count(_field(_field(value, "prompt_tokens_details"), "cached_tokens"))
    if cached is None:
        cached = _count(_field(value, "prompt_cache_hit_tokens"))
    # Anthropic reports uncached input separately from cache creation/reads.
    # OpenAI-compatible prompt_tokens already includes them: never add twice.
    cache_read = _count(_field(value, "cache_read_input_tokens"))
    cache_write = _count(_field(value, "cache_creation_input_tokens"))
    if _field(value, "prompt_tokens") is None and (
        cache_read is not None or cache_write is not None
    ):
        if input_total is not None:
            input_total += (cache_read or 0) + (cache_write or 0)
        cached = cache_read
    reasoning = _count(_field(_field(value, "output_tokens_details"), "reasoning_tokens"))
    if reasoning is None:
        reasoning = _count(_field(_field(value, "completion_tokens_details"), "reasoning_tokens"))
    if reasoning is None:
        reasoning = _count(_field(value, "reasoning_tokens"))
    if input_total is not None and cached is not None and cached > input_total:
        cached = None
    if output_total is not None and reasoning is not None and reasoning > output_total:
        reasoning = None
    return {
        "input_total": input_total,
        "output_total": output_total,
        "cached_input": cached,
        "reasoning_output": reasoning,
        "usage_status": "known"
        if input_total is not None and output_total is not None
        else "partial"
        if input_total is not None or output_total is not None
        else "unknown",
    }


def _path(root: Path, identity: str) -> Path:
    if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Invalid usage request identity")
    return root / ".ultrakb/usage/requests" / f"{identity}.json"


def begin_request(
    model: str, stage: str, *, scope: UsageScope | None = None, **metadata
) -> str | None:
    scope = scope or _SCOPE.get()
    if scope is None:
        return None
    identity = uuid.uuid4().hex
    values = {
        name: getattr(scope, name)
        for name in UsageScope.__dataclass_fields__
        if name not in {"kb_dir", "on_event", "generation", "storage"}
    }
    record = UsageRequest(
        **values,
        **metadata,
        request_attempt_id=identity,
        model=model,
        stage=stage,
        started_at=_now(),
    )
    if scope.storage is not None:
        scope.storage.begin(record)
        return identity
    with (
        usage_scope_lifecycle(scope),
        atomic_record_lock(scope.kb_dir / ".ultrakb/usage/ledger.lock"),
    ):
        from ultrakb.usage_projection import write_request

        write_request(scope.kb_dir, record)
    return identity


def finish_request(
    identity: str | None,
    response: Any = None,
    *,
    state: str = "completed",
    scope: UsageScope | None = None,
    observation: str | None = None,
    terminal: bool = True,
    send_status: str | None = None,
    business_state: str | None = None,
) -> None:
    from ultrakb.lifecycle import KnowledgeBaseRemoved

    scope = scope or _SCOPE.get()
    if identity is None or scope is None:
        return
    try:
        with usage_scope_lifecycle(scope):
            if scope.storage is not None:
                scope.storage.finish(
                    identity,
                    response,
                    state=state,
                    observation=observation,
                    terminal=terminal,
                    send_status=send_status,
                    business_state=business_state,
                )
                return
            _finish_request(
                identity,
                response,
                state=state,
                scope=scope,
                observation=observation,
                terminal=terminal,
                send_status=send_status,
                business_state=business_state,
            )
    except KnowledgeBaseRemoved:
        return  # Late paid receipts cannot mutate a removed or recreated KB.


def _finish_request(
    identity: str,
    response: Any,
    *,
    state: str,
    scope: UsageScope,
    observation: str | None,
    terminal: bool,
    send_status: str | None,
    business_state: str | None,
) -> None:
    path = _path(scope.kb_dir, identity)
    with atomic_record_lock(scope.kb_dir / ".ultrakb/usage/ledger.lock"):
        record = UsageRequest.model_validate_json(path.read_text("utf-8"))
        provider_id = _field(response, "id")
        if (
            isinstance(provider_id, str)
            and record.provider_request_id is not None
            and record.provider_request_id != provider_id
        ):
            logger.warning("Ignored conflicting provider identity for usage request %s", identity)
            return
        usage = normalize_usage(_field(response, "usage"))
        updates: dict[str, Any] = {}
        # Late/repeated callbacks can improve unknown fields, never erase known usage.
        for key, value in usage.items():
            if key != "usage_status" and value is not None and getattr(record, key) is None:
                updates[key] = value
        if terminal and record.ended_at is None:
            updates.update(state=state, ended_at=_now())
        if observation is not None and record.observation != "transport":
            updates["observation"] = observation
        if (
            send_status is not None
            and record.send_status != "sent"
            and (send_status != "unknown" or record.send_status == "unknown")
        ):
            updates["send_status"] = send_status
        if business_state is not None and record.business_state != "cancelled":
            updates["business_state"] = business_state
        if isinstance(provider_id, str):
            updates["provider_request_id"] = provider_id
        merged = record.model_dump() | updates
        merged["usage_status"] = (
            "known"
            if merged["input_total"] is not None and merged["output_total"] is not None
            else "partial"
            if merged["input_total"] is not None or merged["output_total"] is not None
            else "unknown"
        )
        saved = UsageRequest.model_validate(merged)
        from ultrakb.usage_projection import write_request

        write_request(scope.kb_dir, saved)
    if scope.on_event:
        try:
            from ultrakb.usage_projection import progress_receipt

            scope.on_event(
                {
                    "event": "model_usage_progress",
                    "data": progress_receipt(
                        scope.kb_dir,
                        execution_ids=[scope.execution_id],
                        source_ids=[scope.source_id] if scope.source_id else [],
                    ),
                }
            )
        except Exception:
            # Delivery is a projection; its failure must never replay a paid request.
            logger.warning("Import usage progress delivery failed", exc_info=True)


def read_requests(kb_dir: Path) -> tuple[UsageRequest, ...]:
    with atomic_record_lock(kb_dir / ".ultrakb/usage/ledger.lock"):
        return tuple(
            UsageRequest.model_validate_json(path.read_text("utf-8"))
            for path in sorted((kb_dir / ".ultrakb/usage/requests").glob("*.json"))
        )


def aggregate_usage(
    kb_dir: Path,
    *,
    request_ids=None,
    execution_ids=None,
    source_id=None,
    root_import_id=None,
    unit_id=None,
) -> dict:
    return _aggregate_usage(
        read_requests(kb_dir),
        request_ids=request_ids,
        execution_ids=execution_ids,
        source_id=source_id,
        root_import_id=root_import_id,
        unit_id=unit_id,
    )


def _aggregate_usage(
    records,
    *,
    request_ids=None,
    execution_ids=None,
    source_id=None,
    root_import_id=None,
    unit_id=None,
) -> dict:
    """Project filters from one coherent ledger snapshot without more disk reads."""
    request_ids = set(request_ids) if request_ids is not None else None
    execution_ids = set(execution_ids) if execution_ids is not None else None
    records = [
        r
        for r in records
        if (request_ids is None or r.request_attempt_id in request_ids)
        and (execution_ids is None or r.execution_id in execution_ids)
        and (source_id is None or r.source_id == source_id)
        and (root_import_id is None or r.root_import_id == root_import_id)
        and (unit_id is None or r.unit_id == unit_id)
    ]
    totals = {
        key: sum(getattr(r, key) or 0 for r in records)
        for key in ("input_total", "output_total", "cached_input", "reasoning_output")
    }
    missing = {key: sum(getattr(r, key) is None for r in records) for key in totals}
    return {
        **totals,
        "requests": len(records),
        "actual_sends": sum(r.send_status == "sent" for r in records),
        "unsent_attempts": sum(r.send_status == "not_sent" for r in records),
        "send_unknown": sum(r.send_status == "unknown" for r in records),
        "total_known": totals["input_total"] + totals["output_total"],
        "unknown_requests": sum(r.usage_status != "known" for r in records),
        "in_flight_requests": sum(r.state == "started" for r in records),
        "unknown_fields": missing,
        "collection_complete": all(r.observation == "transport" for r in records),
        "usage_complete": all(
            r.usage_status == "known" or r.send_status == "not_sent" for r in records
        ),
        "request_ids": [r.request_attempt_id for r in records],
        "stages": {
            stage: {
                "requests": sum(r.stage == stage for r in records),
                "input_total": sum(r.input_total or 0 for r in records if r.stage == stage),
                "output_total": sum(r.output_total or 0 for r in records if r.stage == stage),
            }
            for stage in sorted({r.stage for r in records})
        },
    }


def mark_source(kb_dir: Path, source_id: str) -> None:
    from ultrakb.source_catalog import read_source
    from ultrakb.unit_publication import list_source_units, read_unit_publication

    path = kb_dir / ".ultrakb/usage/sources" / f"{source_id}.json"
    if path.exists():
        return
    source = read_source(kb_dir, source_id)
    historical = bool(source.legacy_hash)
    for unit in list_source_units(kb_dir, source_id):
        try:
            historical |= bool(
                read_unit_publication(kb_dir, unit.unit_id, "legacy").knowledge_revision_id
            )
        except FileNotFoundError:
            pass
    with atomic_record_lock(kb_dir / ".ultrakb/usage/ledger.lock"):
        if not path.exists():
            atomic_write_json(
                path,
                SourceUsageHistory(
                    history_status="partially_recorded" if historical else "recorded"
                ).model_dump(mode="json"),
            )


def usage_receipt(kb_dir: Path, *, source_id=None, request_ids=None, unit_id=None) -> dict:
    from ultrakb.llm_usage_execution import model_task_receipts

    scope = _SCOPE.get()
    executions = [scope.execution_id] if scope and scope.kb_dir == kb_dir.resolve() else []
    records = read_requests(kb_dir)
    current = _aggregate_usage(records, execution_ids=executions, request_ids=request_ids)
    path = kb_dir / ".ultrakb/usage/sources" / f"{source_id}.json"
    historical = (
        SourceUsageHistory.model_validate_json(path.read_text("utf-8")).history_status
        if path.exists()
        else "unrecorded"
    )
    return {
        "execution_ids": executions,
        "model_tasks": model_task_receipts(kb_dir, executions),
        "source_ids": [source_id] if source_id else [],
        "ledger": str(kb_dir / ".ultrakb/usage"),
        "current": current,
        "cumulative": _aggregate_usage(records, source_id=source_id, unit_id=unit_id)
        if source_id
        else current,
        "history_status": historical if source_id else "execution_only",
    }


def merge_usage_receipts(kb_dir: Path, receipts) -> dict | None:
    from ultrakb.llm_usage_execution import model_task_receipts

    receipts = [receipt for receipt in receipts if receipt]
    if not receipts:
        return None
    records = read_requests(kb_dir)
    ids = {identity for receipt in receipts for identity in receipt["current"]["request_ids"]}
    source_ids = {identity for receipt in receipts for identity in receipt.get("source_ids", [])}
    historical_ids = {r.request_attempt_id for r in records if r.source_id in source_ids}
    executions = {identity for receipt in receipts for identity in receipt["execution_ids"]}
    return {
        "execution_ids": sorted(executions),
        "model_tasks": model_task_receipts(kb_dir, executions),
        "source_ids": sorted(source_ids),
        "ledger": str(kb_dir / ".ultrakb/usage"),
        "current": _aggregate_usage(records, request_ids=ids),
        "cumulative": _aggregate_usage(records, request_ids=historical_ids | ids),
        "history_status": "recorded"
        if all(r["history_status"] == "recorded" for r in receipts)
        else "execution_only"
        if not source_ids
        else "partially_recorded",
    }


_BUDGET_NOTICE = "模型调用预算按逻辑任务累计，不代表资料完成进度。"


def is_model_budget_description(line: str) -> bool:
    """Recognize current and legacy app-generated budget snapshots in receipts."""
    return line == _BUDGET_NOTICE or line.startswith(
        ("模型调用预算：已发送 ", "模型任务累计发送：")
    )


def describe_model_budget(receipt: dict | None) -> tuple[str, ...]:
    tasks = receipt.get("model_tasks", []) if receipt else []
    if not tasks:
        return ()
    return (
        _BUDGET_NOTICE,
        *(
            f"模型调用预算：已发送 {task['actual_sends']} 次 · "
            + (
                "按实际工作量执行，无固定上限"
                if task["limit"] is None
                else f"上限 {task['limit']} 次 · "
                f"剩余 {max(0, task['limit'] - task['actual_sends'])} 次"
            )
            + f"；任务：{task['task_id']}"
            for task in tasks
        ),
    )


def describe_model_usage(receipt: dict | None, *, include_budget: bool = True) -> tuple[str, ...]:
    if receipt is None:
        return ("模型用量：历史未记录",)
    lines = []
    for key, label in (("current", "本次"), ("cumulative", "累计")):
        value = receipt[key]
        lines.append(
            f"模型用量（{label}）：已知输入 {value['input_total']}，"
            f"已知输出 {value['output_total']}，已知合计 {value['total_known']} tokens；"
            f"请求 {value['requests']}，用量未知 {value['unknown_requests']}，"
            f"在途 {value['in_flight_requests']}"
        )
        if value.get("actual_sends") is not None:
            lines.append(
                f"实际发送（{label}）：{value['actual_sends']}；"
                f"发送状态未知：{value.get('send_unknown', 0)}；"
                + ("计量已确认" if value["usage_complete"] else "计量未完整确认")
            )
    if include_budget:
        lines.extend(describe_model_budget(receipt))
    if receipt["history_status"] == "execution_only":
        lines.append("累计范围：本次执行尚未关联来源")
    elif receipt["history_status"] != "recorded":
        lines.append("历史采集状态：" + receipt["history_status"] + "（未记录部分不计作 0）")
    if not receipt["current"]["collection_complete"]:
        lines.append("部分请求仅取得调用层观察，发送层采集完整性未验证")
    lines.extend(
        f"阶段 {stage}：输入 {value['input_total']}，输出 {value['output_total']}，"
        f"请求 {value['requests']}"
        for stage, value in receipt["current"]["stages"].items()
    )
    lines.append("用量账本：" + receipt["ledger"])
    lines.append("执行记录：" + ", ".join(receipt["execution_ids"]))
    return tuple(lines)
