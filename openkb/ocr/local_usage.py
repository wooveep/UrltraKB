"""Durable local OCR attempts and document-wide recognition reservations."""

from __future__ import annotations

import math
import time
import uuid
from pathlib import Path
from typing import Any

from openkb.locks import atomic_write_json
from openkb.sources import SourceStore, SourceVersion, read_object, valid_id


class LocalUsage:
    def __init__(self, store: SourceStore, source: SourceVersion, limits):
        self.store, self.source, self.limits = store, source, limits
        self.regions = self.tokens = 0
        self.record: dict[str, Any] | None = None
        self.path: Path | None = None

    @property
    def remaining(self) -> tuple[int, int]:
        return self.limits.max_regions - self.regions, self.limits.max_tokens - self.tokens

    def begin(self, page: int, profile: dict) -> None:
        regions, tokens = self.remaining
        if regions <= 0 or tokens <= 0:
            raise ValueError("Recognition budget exhausted")
        attempt = uuid.uuid4().hex
        self.path = self.store.owned_path(
            self.store.root / "local-ocr-runs" / self.source.source_id / f"{attempt}.json"
        )
        self.record = {
            "attempt": attempt,
            "source_id": self.source.source_id,
            "version_id": self.source.id,
            "page": page,
            "profile": profile,
            "state": "starting",
            "reaped": False,
            "unknown_usage": 1,
            "regions": regions,
            "reserved_output_tokens": tokens,
            "elapsed_seconds": 0.0,
            "peak_memory_bytes": 0,
            "peak_output_bytes": 0,
        }
        # Unconfirmed execution owns the entire allowance offered to the worker.
        # Only a reaped worker's durable counter can release the unused part.
        atomic_write_json(self.path, self.record)
        self.regions += regions
        self.tokens += tokens
        self.started = time.monotonic()

    def finish(self, output: Path) -> None:
        if self.record is None or self.path is None:
            return
        record = self.record
        record["elapsed_seconds"] = time.monotonic() - self.started
        record["state"] = "unconfirmed"
        receipt = output / "supervision.json"
        if receipt.is_file():
            supervision = read_object(receipt)
            if supervision.get("reaped") is True:
                record.update(
                    reaped=True,
                    state="worker_completed" if supervision.get("exit_code") == 0 else "failed",
                    reason=supervision.get("reason"),
                    exit_code=supervision.get("exit_code"),
                )
                for field in ("peak_memory_bytes", "peak_output_bytes"):
                    value = supervision.get(field)
                    if type(value) is not int or value < 0:
                        raise ValueError("Invalid OCR resource accounting")
                    record[field] = value
                usage_path = output / "usage.json"
                if usage_path.is_file():
                    usage = read_object(usage_path)
                    valid = all(
                        type(usage.get(key)) is int and 0 <= usage[key] <= record[key]
                        for key in ("regions", "reserved_output_tokens")
                    )
                    if valid:
                        self.regions -= record["regions"] - usage["regions"]
                        self.tokens -= (
                            record["reserved_output_tokens"] - usage["reserved_output_tokens"]
                        )
                        record.update(
                            regions=usage["regions"],
                            reserved_output_tokens=usage["reserved_output_tokens"],
                            unknown_usage=0,
                        )
        atomic_write_json(self.store.owned_path(self.path), record)
        self.record = None


def local_ocr_usage(store: SourceStore, source_id: str) -> dict[str, Any]:
    directory = store.owned_path(store.root / "local-ocr-runs" / valid_id(source_id, source=True))
    result: dict[str, Any] = {
        "attempts": 0,
        "unknown_usage": 0,
        "regions": 0,
        "reserved_output_tokens": 0,
        "elapsed_seconds": 0.0,
        "peak_memory_bytes": 0,
        "peak_output_bytes": 0,
        "runs": [],
    }
    for path in sorted(directory.glob("*.json")):
        row = read_object(store.owned_path(path))
        if row.get("source_id") != source_id or row.get("attempt") != path.stem:
            raise ValueError("OCR attempt identity mismatch")
        version = store.version(valid_id(row.get("version_id")))
        if version.source_id != source_id or type(row.get("reaped")) is not bool:
            raise ValueError("Invalid OCR attempt source or cleanup state")
        for key in (
            "unknown_usage",
            "regions",
            "reserved_output_tokens",
            "peak_memory_bytes",
            "peak_output_bytes",
        ):
            value = row.get(key)
            if type(value) is not int or value < 0:
                raise ValueError("Invalid OCR usage")
            if key.startswith("peak_"):
                result[key] = max(result[key], value)
            else:
                result[key] += value
        elapsed = row.get("elapsed_seconds")
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or elapsed < 0
            or not math.isfinite(elapsed)
        ):
            raise ValueError("Invalid OCR elapsed time")
        result["elapsed_seconds"] += elapsed
        result["attempts"] += 1
        result["runs"].append(row)
    return result
