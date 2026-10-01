"""Independent durable request ledger; never stores prompts, bodies or credentials."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from openkb.llm_usage_models import Identity, SourceUsageHistory
from openkb.locks import atomic_record_lock, atomic_write_json

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    request_attempt_id: Identity
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


_SCOPE: ContextVar[UsageScope | None] = ContextVar("openkb_import_usage", default=None)


def active_scope() -> UsageScope | None:
    return _SCOPE.get()


@contextmanager
def usage_context(**fields: Any) -> Iterator[None]:
    scope = _SCOPE.get()
    token = _SCOPE.set(replace(scope, **fields)) if scope else None
    try:
        if scope and fields.get("source_id"):
            mark_source(scope.kb_dir, fields["source_id"])
            from openkb.llm_usage_execution import bind_source_execution

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
    return root / ".openkb/usage/requests" / f"{identity}.json"


def begin_request(model: str, stage: str, *, scope: UsageScope | None = None) -> str | None:
    scope = scope or _SCOPE.get()
    if scope is None:
        return None
    identity = uuid.uuid4().hex
    values = {
        name: getattr(scope, name)
        for name in UsageScope.__dataclass_fields__
        if name not in {"kb_dir", "on_event"}
    }
    record = UsageRequest(
        **values, request_attempt_id=identity, model=model, stage=stage, started_at=_now()
    )
    with atomic_record_lock(scope.kb_dir / ".openkb/usage/ledger.lock"):
        atomic_write_json(_path(scope.kb_dir, identity), record.model_dump(mode="json"))
    return identity


def finish_request(
    identity: str | None,
    response: Any = None,
    *,
    state: str = "completed",
    scope: UsageScope | None = None,
    observation: str | None = None,
) -> None:
    scope = scope or _SCOPE.get()
    if identity is None or scope is None:
        return
    path = _path(scope.kb_dir, identity)
    with atomic_record_lock(scope.kb_dir / ".openkb/usage/ledger.lock"):
        record = UsageRequest.model_validate_json(path.read_text("utf-8"))
        usage = normalize_usage(_field(response, "usage"))
        updates: dict[str, Any] = {}
        # Late/repeated callbacks can improve unknown fields, never erase known usage.
        for key, value in usage.items():
            if key != "usage_status" and value is not None and getattr(record, key) is None:
                updates[key] = value
        if record.ended_at is None:
            updates.update(state=state, ended_at=_now())
        if observation is not None:
            updates["observation"] = observation
        provider_id = _field(response, "id")
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
        atomic_write_json(path, saved.model_dump(mode="json"))
    if scope.on_event:
        try:
            scope.on_event(
                {
                    "event": "model_usage",
                    "data": usage_receipt(scope.kb_dir, source_id=scope.source_id),
                }
            )
        except Exception:
            # Delivery is a projection; its failure must never replay a paid request.
            logger.warning("Import usage progress delivery failed", exc_info=True)


def read_requests(kb_dir: Path) -> tuple[UsageRequest, ...]:
    with atomic_record_lock(kb_dir / ".openkb/usage/ledger.lock"):
        return tuple(
            UsageRequest.model_validate_json(path.read_text("utf-8"))
            for path in sorted((kb_dir / ".openkb/usage/requests").glob("*.json"))
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
    records = [
        r
        for r in read_requests(kb_dir)
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
        "total_known": totals["input_total"] + totals["output_total"],
        "unknown_requests": sum(r.usage_status != "known" for r in records),
        "in_flight_requests": sum(r.state == "started" for r in records),
        "unknown_fields": missing,
        "collection_complete": all(r.observation == "transport" for r in records),
        "usage_complete": all(r.usage_status == "known" for r in records),
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
    from openkb.source_catalog import read_source
    from openkb.source_changes import source_view
    from openkb.unit_publication import list_source_units, read_unit_publication

    path = kb_dir / ".openkb/usage/sources" / f"{source_id}.json"
    if path.exists():
        return
    source = read_source(kb_dir, source_id)
    historical = bool(source.legacy_hash)
    for unit in list_source_units(kb_dir, source_id):
        try:
            historical |= bool(
                read_unit_publication(
                    kb_dir, unit.unit_id, source_view(kb_dir, source)
                ).knowledge_revision_id
            )
        except FileNotFoundError:
            pass
    with atomic_record_lock(kb_dir / ".openkb/usage/ledger.lock"):
        if not path.exists():
            atomic_write_json(
                path,
                SourceUsageHistory(
                    history_status="partially_recorded" if historical else "recorded"
                ).model_dump(mode="json"),
            )


def usage_receipt(kb_dir: Path, *, source_id=None, request_ids=None, unit_id=None) -> dict:
    scope = _SCOPE.get()
    executions = [scope.execution_id] if scope and scope.kb_dir == kb_dir.resolve() else []
    current = aggregate_usage(kb_dir, execution_ids=executions, request_ids=request_ids)
    path = kb_dir / ".openkb/usage/sources" / f"{source_id}.json"
    historical = (
        SourceUsageHistory.model_validate_json(path.read_text("utf-8")).history_status
        if path.exists()
        else "unrecorded"
    )
    return {
        "execution_ids": executions,
        "source_ids": [source_id] if source_id else [],
        "ledger": str(kb_dir / ".openkb/usage"),
        "current": current,
        "cumulative": aggregate_usage(kb_dir, source_id=source_id, unit_id=unit_id)
        if source_id
        else current,
        "history_status": historical if source_id else "execution_only",
    }


def merge_usage_receipts(kb_dir: Path, receipts) -> dict | None:
    receipts = [receipt for receipt in receipts if receipt]
    if not receipts:
        return None
    ids = {identity for receipt in receipts for identity in receipt["current"]["request_ids"]}
    source_ids = {identity for receipt in receipts for identity in receipt.get("source_ids", [])}
    historical_ids = {
        r.request_attempt_id for r in read_requests(kb_dir) if r.source_id in source_ids
    }
    return {
        "execution_ids": sorted(
            {identity for receipt in receipts for identity in receipt["execution_ids"]}
        ),
        "source_ids": sorted(source_ids),
        "ledger": str(kb_dir / ".openkb/usage"),
        "current": aggregate_usage(kb_dir, request_ids=ids),
        "cumulative": aggregate_usage(kb_dir, request_ids=historical_ids | ids),
        "history_status": "recorded"
        if all(r["history_status"] == "recorded" for r in receipts)
        else "execution_only"
        if not source_ids
        else "partially_recorded",
    }


def describe_model_usage(receipt: dict | None) -> tuple[str, ...]:
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
