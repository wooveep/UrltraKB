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
        from openkb.navigation import read_navigation
        from openkb.navigation_usage import navigation_usage

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
            "cloud_jobs": _cloud_jobs(store, source_id),
            "local_ocr": local_ocr_usage(store, source_id),
            "navigation": read_navigation(kb_dir, version),
            "navigation_usage": navigation_usage(store, source_id),
        }


def _cloud_jobs(store: SourceStore, source_id: str) -> list[dict[str, Any]]:
    """Expose durable requests, including failed downloads and uncertain POSTs."""
    result = []
    for path in sorted(store.owned_path(store.root / "cloud-jobs").glob("*.json")):
        job = read_object(store.owned_path(path))
        value = job.get("input")
        if not isinstance(value, dict):
            raise ValueError("Invalid cloud job input")
        version = store.version(valid_id(value.get("source")))
        if version.source_id != source_id:
            continue
        counts = {}
        for field in ("requests", "submissions", "download_bytes"):
            count = job.get(field, 0)
            if type(count) is not int or count < 0:
                raise ValueError("Invalid cloud job accounting")
            counts[field] = count
        result.append(
            {
                "version_id": version.id,
                "page": value.get("page"),
                "identity": job.get("identity"),
                "job_id": job.get("job_id"),
                "state": job.get("state"),
                "remote_state": job.get("remote_state"),
                "reason": job.get("reason"),
                **counts,
            }
        )
    return result
