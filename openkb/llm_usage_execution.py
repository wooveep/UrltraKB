"""Application execution boundaries and projections for the request ledger."""

from __future__ import annotations

import inspect
import re
import uuid
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
from pathlib import Path

from openkb.lifecycle import async_read_lifecycle, read_lifecycle
from openkb.llm_usage import (
    _SCOPE,
    UsageScope,
    aggregate_usage,
    merge_usage_receipts,
    usage_context,
    usage_receipt,
)
from openkb.llm_usage_models import UsageExecution
from openkb.locks import atomic_record_lock, atomic_write_json


def _read_execution(path: Path) -> UsageExecution:
    record = UsageExecution.model_validate_json(path.read_text("utf-8"))
    if record.execution_id != path.stem:
        raise ValueError("Usage execution does not match its record identity")
    return record


def _write_execution(path: Path, record: UsageExecution, **updates) -> None:
    updated = UsageExecution.model_validate(record.model_dump() | updates)
    atomic_write_json(path, updated.model_dump(mode="json"))


@contextmanager
def import_usage_execution(
    kb_dir: Path,
    operation: str,
    *,
    execution_id: str | None = None,
    task_id=None,
    on_event=None,
    cancelled=None,
    on_wait=None,
):
    root = kb_dir.resolve()
    # Receipts survive business rollback, but share the import's directory lifetime.
    with read_lifecycle(root, cancelled=cancelled, on_wait=on_wait):
        existing = _SCOPE.get()
        if existing and existing.kb_dir == root:
            yield existing
            return
        scope = UsageScope(root, execution_id or uuid.uuid4().hex, on_event=on_event)
        record = UsageExecution(
            execution_id=scope.execution_id, operation=operation, state="started", task_id=task_id
        )
        path = root / ".openkb/usage/executions" / f"{scope.execution_id}.json"
        _write_execution(path, record)
        token = _SCOPE.set(scope)
        state = "completed"
        try:
            yield scope
        except BaseException:
            state = "interrupted"
            raise
        finally:
            try:
                with atomic_record_lock(root / ".openkb/usage/ledger.lock"):
                    _write_execution(path, _read_execution(path), state=state)
            finally:
                _SCOPE.reset(token)


def bind_pending_execution(intent_id: str, attempt_id: str) -> None:
    """Persist delivery identity before model work, outside pending rollback."""
    scope = _SCOPE.get()
    if scope:
        path = scope.kb_dir / ".openkb/usage/executions" / f"{scope.execution_id}.json"
        with atomic_record_lock(scope.kb_dir / ".openkb/usage/ledger.lock"):
            _write_execution(
                path,
                _read_execution(path),
                pending_intent_id=intent_id,
                pending_attempt_id=attempt_id,
            )


def bind_source_execution(source_id: str) -> None:
    """Retain source association even when an execution sends no model requests."""
    scope = _SCOPE.get()
    if scope:
        path = scope.kb_dir / ".openkb/usage/executions" / f"{scope.execution_id}.json"
        with atomic_record_lock(scope.kb_dir / ".openkb/usage/ledger.lock"):
            record = _read_execution(path)
            if source_id not in record.source_ids:
                _write_execution(path, record, source_ids=sorted((*record.source_ids, source_id)))


def _native_source_id(identity):
    # Legacy hashes acquire a catalog identity during admission; do not bind the hash.
    return (
        identity if isinstance(identity, str) and re.fullmatch(r"[a-f0-9]{32}", identity) else None
    )


def _project(root, result, before):
    scope = _SCOPE.get()
    after = aggregate_usage(root, execution_ids=[scope.execution_id])["request_ids"]
    ids = set(after) - set(before)
    source_id = _native_source_id(
        result.get("source_id") if isinstance(result, dict) else getattr(result, "source_id", None)
    )
    previous = (
        result.get("model_usage")
        if isinstance(result, dict)
        else getattr(result, "model_usage", None)
    )
    if source_id is None and previous and len(previous["source_ids"]) == 1:
        source_id = _native_source_id(previous["source_ids"][0])
    source_id = source_id or scope.source_id
    with usage_context(source_id=source_id):
        receipt = usage_receipt(root, source_id=source_id, request_ids=ids)
    if isinstance(result, dict):
        return {**result, "model_usage": receipt}
    if hasattr(result, "model_usage"):
        units = tuple(
            replace(
                unit,
                model_usage=usage_receipt(
                    root,
                    source_id=source_id,
                    unit_id=unit.unit_id,
                    request_ids=set(
                        aggregate_usage(root, request_ids=ids, unit_id=unit.unit_id)["request_ids"]
                    ),
                ),
            )
            for unit in result.units
        )
        return replace(result, model_usage=receipt, units=units)
    return result


def track_import_usage(function):
    """Nested public calls share an execution, with per-call request-set receipts."""
    signature = inspect.signature(function)

    def enter(args, kwargs):
        bound = signature.bind(*args, **kwargs)
        root = Path(bound.arguments["kb_dir"]).resolve()
        context = bound.arguments.get("context")
        return (
            root,
            _native_source_id(bound.arguments.get("source_id")),
            context,
            bound.arguments.get("on_event"),
        )

    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def async_run(*args, **kwargs):
            root, source_id, context, on_event = enter(args, kwargs)
            if not (root / ".openkb/config.yaml").is_file():
                return await function(*args, **kwargs)
            async with async_read_lifecycle(
                root,
                cancelled=getattr(context, "cancelled", None),
                on_wait=getattr(context, "waiting", None),
            ):
                with (
                    import_usage_execution(
                        root,
                        function.__name__,
                        task_id=getattr(context, "usage_task_id", None),
                        on_event=on_event or getattr(context, "on_event", None),
                    ) as scope,
                    usage_context(source_id=source_id),
                ):
                    before = aggregate_usage(root, execution_ids=[scope.execution_id])[
                        "request_ids"
                    ]
                    result = await function(*args, **kwargs)
                    return _project(root, result, before)

        return async_run

    @wraps(function)
    def run(*args, **kwargs):
        root, source_id, context, on_event = enter(args, kwargs)
        if not (root / ".openkb/config.yaml").is_file():
            return function(*args, **kwargs)
        with (
            import_usage_execution(
                root,
                function.__name__,
                task_id=getattr(context, "usage_task_id", None),
                on_event=on_event or getattr(context, "on_event", None),
                cancelled=getattr(context, "cancelled", None),
                on_wait=getattr(context, "waiting", None),
            ) as scope,
            usage_context(source_id=source_id),
        ):
            before = aggregate_usage(root, execution_ids=[scope.execution_id])["request_ids"]
            result = function(*args, **kwargs)
            return _project(root, result, before)

    return run


def task_usage_receipt(kb_dir: Path, task_id: str):
    return _execution_receipt(
        kb_dir,
        [
            record.execution_id
            for path in (kb_dir / ".openkb/usage/executions").glob("*.json")
            if (record := _read_execution(path)).task_id == task_id
        ],
    )


def pending_usage_receipt(kb_dir: Path, intent_id: str, attempt_id: str | None):
    return _execution_receipt(
        kb_dir,
        [
            record.execution_id
            for path in (kb_dir / ".openkb/usage/executions").glob("*.json")
            if (record := _read_execution(path)).pending_intent_id == intent_id
            and record.pending_attempt_id == attempt_id
        ],
    )


def _execution_receipt(kb_dir: Path, ids):
    from openkb.llm_usage import read_requests

    if not ids:
        return None
    current = aggregate_usage(kb_dir, execution_ids=ids)
    sources = {
        source_id
        for identity in ids
        for source_id in _read_execution(
            kb_dir / ".openkb/usage/executions" / f"{identity}.json"
        ).source_ids
    }
    sources.update(
        r.source_id for r in read_requests(kb_dir) if r.execution_id in ids and r.source_id
    )
    receipts = [usage_receipt(kb_dir, source_id=source) for source in sorted(sources)]
    cumulative = merge_usage_receipts(kb_dir, receipts)
    return {
        "execution_ids": sorted(ids),
        "source_ids": sorted(sources),
        "ledger": str(kb_dir / ".openkb/usage"),
        "current": current,
        "cumulative": cumulative["cumulative"] if cumulative else current,
        "history_status": cumulative["history_status"] if cumulative else "execution_only",
    }


def track_import_unit(function):
    @wraps(function)
    def run(kb_dir, prepared, **kwargs):
        admission = kwargs["admission"]
        with usage_context(
            source_id=admission.source.source_id,
            source_revision_id=admission.revision.source_revision_id,
            root_import_id=admission.discovery_intent.root_import_id,
        ):
            return function(kb_dir, prepared, **kwargs)

    return run
