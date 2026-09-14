"""Durable source outcomes and cumulative usage across explicit continuations."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from openkb.application.documents import DocumentResult
from openkb.locks import atomic_write_json, kb_ingest_lock, kb_read_lock
from openkb.ocr.history import cloud_jobs
from openkb.ocr.local_usage import local_ocr_usage
from openkb.sources import SourceStore, read_object, valid_id

_DEFER: ContextVar[bool] = ContextVar("openkb_defer_source_result", default=False)


def source_results_deferred() -> bool:
    return _DEFER.get()


@contextmanager
def defer_source_results():
    """An enclosing operation owns the final outcome and settled budget."""
    token = _DEFER.set(True)
    try:
        yield
    finally:
        _DEFER.reset(token)


def finish_source_result(kb_dir: Path, result: DocumentResult) -> DocumentResult:
    from openkb.locks import LockCancelled
    from openkb.mutation import RecoveryRequired

    if _DEFER.get() or result.source_id is None:
        return result
    try:
        record_source_result(kb_dir, result)
    except (LockCancelled, RecoveryRequired):
        if result.knowledge_compilation != "completed":
            raise
    except Exception:
        pass
    else:
        return result
    return replace(result, warnings=(*result.warnings, "source_outcome_record_failed"))


def record_source_result(kb_dir: Path, result: DocumentResult) -> None:
    if result.source_id is None:
        return
    store = SourceStore(kb_dir)
    directory = store.owned_path(store.root / "runs" / valid_id(result.source_id, source=True))
    attempt = uuid.uuid4().hex
    record = directory / f"{attempt}.json"
    latest = directory / "latest.json"
    # The JSON conversion is intentionally after the execution budget closes;
    # its final accounting updates the report dictionary during scope exit.
    with kb_ingest_lock(kb_dir / ".openkb"):
        # Like runtime receipts, these facts survive a stop after business work
        # has unwound. Publish the immutable record before its atomic pointer;
        # a crash cannot expose a pointer to a partially written outcome.
        atomic_write_json(store.owned_path(record), asdict(result))
        atomic_write_json(store.owned_path(latest), {"attempt": attempt})


def source_status(kb_dir: Path, source_id: str) -> dict[str, Any]:
    with kb_read_lock(kb_dir / ".openkb"):
        store = SourceStore(kb_dir)
        version = store.current(source_id)
        directory = store.owned_path(store.root / "runs" / valid_id(source_id, source=True))
        latest = store.owned_path(directory / "latest.json")
        current = None
        if latest.exists():
            record = read_object(latest)
            if set(record) != {"attempt"}:
                raise ValueError("Invalid source outcome pointer")
            attempt = valid_id(record["attempt"], source=True)
            current = DocumentResult.from_summary(
                read_object(store.owned_path(directory / f"{attempt}.json"))
            )
            if current.source_id != source_id:
                raise ValueError("Source outcome identity mismatch")
        totals: dict[str, Any] = {
            "runs": 0,
            "observable_attempts": 0,
            "charged_tokens": 0,
            "unknown_usage": 0,
            "elapsed_seconds": 0.0,
        }
        reported_ids: set[str] = set()
        for path in directory.glob("*.json"):
            if path.name == "latest.json":
                continue
            valid_id(path.stem, source=True)
            result = DocumentResult.from_summary(read_object(store.owned_path(path)))
            if result.source_id != source_id:
                raise ValueError("Source history identity mismatch")
            totals["runs"] += 1
            for field in totals.keys() - {"runs"}:
                totals[field] += result.usage.get(field, 0)
            reported_ids.update(
                row["id"] for row in result.usage.get("measurement", {}).get("requests", [])
            )
        from openkb.source_request_journal import unreported_source_usage

        unreported, pending_requests = unreported_source_usage(store, source_id, reported_ids)
        for field in totals:
            totals[field] += unreported[field]
        from openkb.navigation import read_navigation
        from openkb.navigation_usage import navigation_usage

        indexed_usage = navigation_usage(store, source_id)
        for field in totals:
            totals[field] += indexed_usage["standalone"].get(field, 0)

        # Embedded documents are imported during the parent's parse. Their
        # selected evidence is readable before they have a compilation run.
        if current is None and version.origin.startswith("attachment:"):
            from openkb.evidence import ParseStore

            parses = ParseStore(kb_dir)
            parsed = parses.selected(version)
            if parsed is not None:
                complete = parses.complete(version, parsed)
                current = DocumentResult(
                    version.origin,
                    "unfinished",
                    (str(store.original(version)),),
                    quality=tuple(
                        q["reason"] for q in parsed.quality if q["status"] == "needs_review"
                    ),
                    input_version=version.id,
                    source_intake="saved",
                    knowledge_compilation="not_started",
                    stage="parsed" if complete else "parsing",
                    reason="knowledge_compilation_pending"
                    if complete
                    else "source_quality_needs_review",
                    resume=version.id,
                    source_id=source_id,
                    parse_id=parsed.id,
                )
        return {
            "source": asdict(version),
            "original": str(store.original(version)),
            "result": asdict(current) if current and current.input_version == version.id else None,
            "cumulative_usage": totals,
            "unconfirmed_requests": pending_requests,
            "cloud_jobs": cloud_jobs(store, source_id),
            "local_ocr": local_ocr_usage(store, source_id),
            "navigation": read_navigation(kb_dir, version),
            "navigation_usage": indexed_usage,
        }
