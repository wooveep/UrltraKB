"""Metadata-only timing for one execution; overlapping spans are never added as wall time."""

from __future__ import annotations

import math
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_ACTIVE: ContextVar[Measurement | None] = ContextVar("execution_measurement", default=None)
_PARENT: ContextVar[str | None] = ContextVar("measurement_parent", default=None)
_OPERATION: ContextVar[str] = ContextVar("measurement_operation", default="")
_GROUP: ContextVar[str | None] = ContextVar("measurement_document_group", default=None)


class Measurement:
    def __init__(self):
        self.started = time.monotonic()
        self.identity = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.active_requests = 0
        self.analysis_events = set()
        self.peak_rss_bytes: int | None = None
        self.peak_inflight_tokens = 0
        self.evidence_groups: int | None = None
        self.planned_pages: int | None = None
        self.first_inspectable_seconds: float | None = None
        self.group_elapsed: dict[str, float] = {}
        self.value: dict[str, Any] = {
            "schema": 4,
            "spans": [],
            "requests": [],
            "peak_concurrency": 0,
            "analyses": [],
            "document_planning": [],
            "summary": self._summary(),
        }
        self._sample_memory()

    def _sample_memory(self) -> None:
        """Retain only an observed resident-memory peak, never a guessed zero."""

        try:
            from openkb.resource_memory import memory_sample

            sample = memory_sample()
            resident = sample.get("resident") if isinstance(sample, dict) else None
            if type(resident) is int and resident >= 0:
                self.peak_rss_bytes = max(self.peak_rss_bytes or 0, resident)
        except (OSError, ValueError):
            return

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        offset = (len(values) - 1) * percentile
        lower, upper = math.floor(offset), math.ceil(offset)
        if lower == upper:
            return values[lower]
        return values[lower] + (values[upper] - values[lower]) * (offset - lower)

    def _summary(self) -> dict[str, Any]:
        rows = self.value.get("requests", []) if hasattr(self, "value") else []
        completed = sorted(
            row["request_seconds"]
            for row in rows
            if row.get("transport_complete")
            and isinstance(row.get("request_seconds"), (int, float))
        )

        def total(field: str) -> int | None:
            values = [row.get(field) for row in rows]
            return sum(values) if values and all(type(value) is int for value in values) else None

        requests_by_group: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            group = row.get("group")
            if isinstance(group, str):
                requests_by_group.setdefault(group, []).append(row)
        group_usage = []
        for group, elapsed in sorted(self.group_elapsed.items()):
            group_requests = requests_by_group.get(group, [])

            def group_total(field: str) -> int | None:
                values = [row.get(field) for row in group_requests]
                return (
                    sum(values) if values and all(type(value) is int for value in values) else None
                )

            group_usage.append(
                {
                    "group": group,
                    "elapsed_seconds": elapsed,
                    "requests": len(group_requests),
                    "generation_requests": sum(
                        row["stage"] == "generation" for row in group_requests
                    ),
                    "verification_requests": sum(
                        row["stage"] == "verification" for row in group_requests
                    ),
                    "correction_requests": sum(
                        row["stage"] == "correction" for row in group_requests
                    ),
                    "input_tokens": group_total("input_tokens"),
                    "output_tokens": group_total("output_tokens"),
                }
            )

        planning = self.value.get("document_planning", []) if hasattr(self, "value") else []
        accepted = [row for row in planning if row["event"] == "accepted"]
        analyses = self.value.get("analyses", []) if hasattr(self, "value") else []
        document = {
            "evidence_groups": self.evidence_groups,
            "planning_calls": len(accepted),
            "extra_planning_calls": max(0, len(accepted) - (self.evidence_groups or 0))
            + sum(row["event"] in {"split", "retry"} for row in planning),
            "range_carry_count": sum(
                max(0, len(row["completed_ranges"]) - len(row["target_ranges"])) for row in accepted
            ),
            "planned_pages": self.planned_pages,
            "http_attempts": len(rows),
            "local_result_adoptions": sum(
                row["event"] == "adopted" or (row["event"] == "accepted" and row["cached"])
                for row in planning
            )
            + sum(row["event"] == "hit" for row in analyses),
            "first_inspectable_seconds": self.first_inspectable_seconds,
            "wall_seconds": time.monotonic() - self.started,
            "group_usage": group_usage,
            # Model pricing is not part of this receipt. Unknown is not zero.
            "cost_usd": None,
        }

        return {
            "request_p50_seconds": self._percentile(completed, 0.5),
            "request_p95_seconds": self._percentile(completed, 0.95),
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_inflight_tokens": self.peak_inflight_tokens,
            "input_tokens_total": total("input_tokens"),
            "output_tokens_total": total("output_tokens"),
            "document": document,
        }

    def finalize(self) -> None:
        """Refresh aggregate observations after a processing scope settles."""

        with self.lock:
            self._sample_memory()
            self.value["summary"] = self._summary()

    def begin_request(self, observation, queue_seconds, preparation_seconds, *, options=None):
        with self.lock:
            row = {
                "id": f"{self.identity}:{observation['attempt']}",
                "stage": observation["stage"],
                "operation": _OPERATION.get() or observation["stage"],
                "group": _GROUP.get(),
                "parent": _PARENT.get(),
                "started_seconds": time.monotonic() - self.started,
                "queue_seconds": queue_seconds,
                "preparation_seconds": preparation_seconds,
                "request_seconds": 0.0,
                "transport_complete": False,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "input_tokens": None,
                "cache_miss_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "input_reservation": observation.get("input_estimate"),
                "output_reservation": observation.get("output_reserve"),
                "provider_model": None,
                "system_fingerprint": None,
                "effective_options": effective_options(options or {}),
            }
            self.value["requests"].append(row)
            self._sample_memory()
            self.active_requests += 1
            self.value["peak_concurrency"] = max(
                self.value["peak_concurrency"], self.active_requests
            )
            return row

    def finish_request(self, row):
        with self.lock:
            if row["transport_complete"]:
                return
            row["request_seconds"] = time.monotonic() - self.started - row["started_seconds"]
            row["transport_complete"] = True
            self.active_requests -= 1
            self._sample_memory()

    def observe_pending(self, row):
        with self.lock:
            if not row["transport_complete"]:
                # This is a lower bound until the transport actually finishes.
                row["request_seconds"] = time.monotonic() - self.started - row["started_seconds"]

    def provider_usage(self, row, usage, *, response=None):
        details = getattr(usage, "prompt_tokens_details", None)

        def count(*values):
            return next((v for v in values if type(v) is int and v >= 0), None)

        with self.lock:
            row["cache_read_tokens"] = count(
                getattr(details, "cached_tokens", None),
                getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "prompt_cache_hit_tokens", None),
            )
            row["cache_write_tokens"] = count(
                getattr(details, "cache_creation_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None),
            )
            row["input_tokens"] = count(getattr(usage, "prompt_tokens", None))
            row["output_tokens"] = count(getattr(usage, "completion_tokens", None))
            row["reasoning_tokens"] = count(
                getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
            )
            row["cache_miss_tokens"] = count(getattr(usage, "prompt_cache_miss_tokens", None))
            if (
                row["cache_miss_tokens"] is None
                and row["input_tokens"] is not None
                and row["cache_read_tokens"] is not None
                and row["cache_read_tokens"] <= row["input_tokens"]
            ):
                row["cache_miss_tokens"] = row["input_tokens"] - row["cache_read_tokens"]
            for field in ("provider_model", "system_fingerprint"):
                value = getattr(response, "model" if field == "provider_model" else field, None)
                row[field] = value if isinstance(value, str) else None


def effective_options(options):
    """Retain known model controls only; never headers, credentials, messages or callbacks."""
    selected = {
        key: options[key]
        for key in (
            "model",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "seed",
            "reasoning_effort",
            "timeout",
            "response_format",
        )
        if key in options
    }
    extra = options.get("extra_body")
    if isinstance(extra, dict) and "reasoning_effort" in extra:
        selected["reasoning_effort"] = extra["reasoning_effort"]
    if isinstance(extra, dict) and isinstance(extra.get("thinking"), dict):
        selected["thinking"] = {
            key: value
            for key, value in extra["thinking"].items()
            if key in {"type", "budget_tokens"}
        }
    return selected


@contextmanager
def measurement_scope(measurement):
    token = _ACTIVE.set(measurement)
    parent = _PARENT.set(None)
    try:
        yield
    finally:
        _PARENT.reset(parent)
        _ACTIVE.reset(token)


@contextmanager
def document_group_scope(group: str):
    """Attribute page work and its physical model attempts to one plan group."""

    measurement = _ACTIVE.get()
    token = _GROUP.set(group)
    started = time.monotonic()
    try:
        yield
    finally:
        _GROUP.reset(token)
        if measurement is not None:
            with measurement.lock:
                measurement.group_elapsed[group] = measurement.group_elapsed.get(group, 0.0) + (
                    time.monotonic() - started
                )


def record_document_totals(*, evidence_groups: int | None = None, planned_pages: int | None = None):
    measurement = _ACTIVE.get()
    if measurement is None:
        return
    with measurement.lock:
        if evidence_groups is not None:
            measurement.evidence_groups = evidence_groups
        if planned_pages is not None:
            measurement.planned_pages = planned_pages


def record_inflight_tokens(tokens: int) -> None:
    """Observe an atomic shared-pool reservation, not provider-billed usage."""

    measurement = _ACTIVE.get()
    if measurement is not None:
        with measurement.lock:
            measurement.peak_inflight_tokens = max(measurement.peak_inflight_tokens, tokens)


@contextmanager
def measure_span(stage):
    measurement = _ACTIVE.get()
    if measurement is None:
        yield
        return
    with measurement.lock:
        row = {
            "id": f"span:{len(measurement.value['spans']) + 1}",
            "stage": stage,
            "parent": _PARENT.get(),
            "started_seconds": time.monotonic() - measurement.started,
            "elapsed_seconds": 0.0,
            "status": "running",
        }
        measurement.value["spans"].append(row)
    token = _PARENT.set(row["id"])
    try:
        yield
        row["status"] = "completed"
    except BaseException:
        row["status"] = "interrupted"
        raise
    finally:
        row["elapsed_seconds"] = time.monotonic() - measurement.started - row["started_seconds"]
        _PARENT.reset(token)


def current_operation():
    return _OPERATION.get()


@contextmanager
def request_operation(name):
    token = _OPERATION.set(name)
    try:
        yield
    finally:
        _OPERATION.reset(token)


def record_analysis(stage, event, identity, seconds=0.0, reason=""):
    measurement = _ACTIVE.get()
    if measurement is None:
        return
    with measurement.lock:
        key = stage, event, identity
        if event != "binding" and key in measurement.analysis_events:
            return
        measurement.analysis_events.add(key)
        measurement.value["analyses"].append(
            {
                "stage": stage,
                "event": event,
                "id": identity,
                "elapsed_seconds": seconds,
                "reason": reason,
            }
        )


_DOCUMENT_PLANNING_FIELDS = {
    "event",
    "window",
    "total_windows",
    "frozen_evidence",
    "target_ranges",
    "completed_ranges",
    "carry_pages",
    "carry_unresolved",
    "pages",
    "unresolved",
    "checkpoint",
    "result",
    "attempt",
    "cached",
    "reason",
    "parts",
    "frozen_prefix_hash",
    "frozen_prefix_blocks",
    "frozen_prefix_chars",
    "candidate_count",
    "frozen_last_use_seconds",
    "elapsed_seconds",
    "request_id",
    "input_reservation",
    "output_reservation",
    "actual_input",
    "actual_output",
    "input_estimate_error",
    "output_estimate_error",
}


def _valid_document_range(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return (
            len(value) == 2
            and all(type(item) is int and item >= 0 for item in value)
            and value[0] < value[1]
        )
    if isinstance(value, dict):
        return (
            set(value) == {"block_index", "start_char", "end_char"}
            and all(type(value[key]) is int and value[key] >= 0 for key in value)
            and value["start_char"] < value["end_char"]
        )
    return False


def _valid_document_planning_row(row: Any) -> bool:
    if not isinstance(row, dict) or set(row) != _DOCUMENT_PLANNING_FIELDS:
        return False
    if row["event"] not in {"accepted", "split", "retry", "resume", "adopted"}:
        return False
    if not all(
        type(row[key]) is int and row[key] >= 0
        for key in (
            "window",
            "total_windows",
            "carry_pages",
            "carry_unresolved",
            "pages",
            "unresolved",
            "attempt",
            "parts",
            "frozen_prefix_blocks",
            "frozen_prefix_chars",
            "candidate_count",
        )
    ):
        return False
    if type(row["cached"]) is not bool or not isinstance(row["reason"], str):
        return False
    if not all(
        isinstance(row[key], str) and row[key] for key in ("frozen_evidence", "frozen_prefix_hash")
    ):
        return False
    if any(
        not isinstance(row[key], list)
        or any(not _valid_document_range(value) for value in row[key])
        for key in ("target_ranges", "completed_ranges")
    ):
        return False
    if not all(
        row[key] is None or isinstance(row[key], str)
        for key in ("checkpoint", "result", "request_id")
    ):
        return False
    if not all(
        row[key] is None or (type(row[key]) is int and row[key] >= 0)
        for key in ("input_reservation", "output_reservation", "actual_input", "actual_output")
    ):
        return False
    if not all(
        row[key] is None or type(row[key]) is int
        for key in ("input_estimate_error", "output_estimate_error")
    ):
        return False
    return all(
        type(row[key]) in (int, float) and math.isfinite(row[key]) and row[key] >= 0
        for key in ("frozen_last_use_seconds", "elapsed_seconds")
    )


def record_document_planning(row: dict[str, Any]) -> None:
    """Persist compact W/S/T planning telemetry, never the evidence itself."""

    measurement = _ACTIVE.get()
    if measurement is None:
        return
    if not _valid_document_planning_row(row):
        raise ValueError("Invalid document-planning observation")
    with measurement.lock:
        measurement.value["document_planning"].append(dict(row))
        if (
            row["event"] in {"accepted", "adopted"}
            and measurement.first_inspectable_seconds is None
        ):
            measurement.first_inspectable_seconds = time.monotonic() - measurement.started


def measurement_identity():
    measurement = _ACTIVE.get()
    return measurement.identity if measurement else None


def request_marker() -> int:
    """Mark the current request stream before one synchronous protocol call."""

    measurement = _ACTIVE.get()
    if measurement is None:
        return 0
    with measurement.lock:
        return len(measurement.value["requests"])


def request_after(marker: int, stage: str) -> dict[str, Any] | None:
    """Return a source-free link to the latest request started after ``marker``."""

    measurement = _ACTIVE.get()
    if measurement is None or type(marker) is not int or marker < 0:
        return None
    with measurement.lock:
        rows = measurement.value["requests"][marker:]
        row = next((item for item in reversed(rows) if item.get("stage") == stage), None)
        if row is None:
            return None
        return {
            key: row.get(key)
            for key in (
                "id",
                "input_reservation",
                "output_reservation",
                "input_tokens",
                "output_tokens",
            )
        }


def _valid_document_summary(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "evidence_groups",
        "planning_calls",
        "extra_planning_calls",
        "range_carry_count",
        "planned_pages",
        "http_attempts",
        "local_result_adoptions",
        "first_inspectable_seconds",
        "wall_seconds",
        "group_usage",
        "cost_usd",
    }:
        return False
    for key in ("evidence_groups", "planned_pages"):
        if value[key] is not None and (type(value[key]) is not int or value[key] < 0):
            return False
    for key in (
        "planning_calls",
        "extra_planning_calls",
        "range_carry_count",
        "http_attempts",
        "local_result_adoptions",
    ):
        if type(value[key]) is not int or value[key] < 0:
            return False
    for key in ("first_inspectable_seconds", "wall_seconds", "cost_usd"):
        number = value[key]
        if number is not None and (
            type(number) not in (int, float) or not math.isfinite(number) or number < 0
        ):
            return False
    if not isinstance(value["group_usage"], list):
        return False
    for row in value["group_usage"]:
        if not isinstance(row, dict) or set(row) != {
            "group",
            "elapsed_seconds",
            "requests",
            "generation_requests",
            "verification_requests",
            "correction_requests",
            "input_tokens",
            "output_tokens",
        }:
            return False
        if not isinstance(row["group"], str) or not row["group"]:
            return False
        if (
            type(row["elapsed_seconds"]) not in (int, float)
            or not math.isfinite(row["elapsed_seconds"])
            or row["elapsed_seconds"] < 0
        ):
            return False
        for key in (
            "requests",
            "generation_requests",
            "verification_requests",
            "correction_requests",
        ):
            if type(row[key]) is not int or row[key] < 0:
                return False
        for key in ("input_tokens", "output_tokens"):
            if row[key] is not None and (type(row[key]) is not int or row[key] < 0):
                return False
    return True


def validate_measurement(value):
    if not isinstance(value, dict) or type(value.get("schema")) is not int:
        raise ValueError("Invalid execution measurement")
    legacy_fields = {"schema", "spans", "requests", "peak_concurrency"}
    analysis_fields = legacy_fields | {"analyses"}
    current_fields = analysis_fields | {"document_planning"}
    summary_fields = current_fields | {"summary"}
    if (
        (value["schema"] == 1 and set(value) not in {legacy_fields, analysis_fields})
        or (value["schema"] == 2 and set(value) != current_fields)
        or (value["schema"] in {3, 4} and set(value) != summary_fields)
        or value["schema"] not in {1, 2, 3, 4}
    ):
        raise ValueError("Invalid execution measurement")
    if type(value["peak_concurrency"]) is not int or value["peak_concurrency"] < 0:
        raise ValueError("Invalid measured concurrency")
    analyses = value.get("analyses", [])
    if not isinstance(analyses, list):
        raise ValueError("Invalid analysis observations")
    for row in analyses:
        if (
            not isinstance(row, dict)
            or set(row) != {"stage", "event", "id", "elapsed_seconds", "reason"}
            or row["event"] not in {"hit", "miss", "invalid", "produced", "binding"}
            or not all(isinstance(row[k], str) for k in ("stage", "id", "reason"))
            or type(row["elapsed_seconds"]) not in (int, float)
            or not math.isfinite(row["elapsed_seconds"])
            or row["elapsed_seconds"] < 0
        ):
            raise ValueError("Invalid analysis observation")
    planning = value.get("document_planning", [])
    if not isinstance(planning, list) or any(
        not _valid_document_planning_row(row) for row in planning
    ):
        raise ValueError("Invalid document-planning observations")
    if value["schema"] in {3, 4}:
        summary = value["summary"]
        old_summary = {
            "request_p50_seconds",
            "request_p95_seconds",
            "peak_rss_bytes",
            "input_tokens_total",
            "output_tokens_total",
        }
        fields = (
            old_summary
            if value["schema"] == 3
            else old_summary
            | {
                "peak_inflight_tokens",
                "document",
            }
        )
        if not isinstance(summary, dict) or set(summary) != fields:
            raise ValueError("Invalid execution measurement summary")
        for key in ("request_p50_seconds", "request_p95_seconds"):
            if summary[key] is not None and (
                type(summary[key]) not in (int, float)
                or not math.isfinite(summary[key])
                or summary[key] < 0
            ):
                raise ValueError("Invalid execution latency summary")
        for key in ("peak_rss_bytes", "input_tokens_total", "output_tokens_total"):
            if summary[key] is not None and (type(summary[key]) is not int or summary[key] < 0):
                raise ValueError("Invalid execution usage summary")
        if value["schema"] == 4:
            if (
                type(summary["peak_inflight_tokens"]) is not int
                or summary["peak_inflight_tokens"] < 0
            ):
                raise ValueError("Invalid inflight reservation peak")
            if not _valid_document_summary(summary["document"]):
                raise ValueError("Invalid document execution summary")
    for field in ("spans", "requests"):
        if not isinstance(value[field], list):
            raise ValueError("Invalid measured records")
        seen = set()
        for row in value[field]:
            common = {"id", "stage", "parent", "started_seconds"}
            extras = (
                {"elapsed_seconds", "status"}
                if field == "spans"
                else {
                    "operation",
                    "queue_seconds",
                    "preparation_seconds",
                    "request_seconds",
                    "cache_read_tokens",
                    "cache_write_tokens",
                }
            )
            metadata = {
                "input_tokens",
                "cache_miss_tokens",
                "output_tokens",
                "reasoning_tokens",
                "input_reservation",
                "output_reservation",
                "provider_model",
                "system_fingerprint",
                "effective_options",
                "response_activity",
                "group",
            }
            if not isinstance(row, dict) or (set(row) - metadata) not in (
                common | extras,
                common | extras | ({"transport_complete"} if field == "requests" else set()),
            ):
                raise ValueError("Invalid measured record fields")
            if "transport_complete" in row and type(row["transport_complete"]) is not bool:
                raise ValueError("Invalid transport completion state")
            if not all(isinstance(row[k], str) and row[k] for k in ("id", "stage")):
                raise ValueError("Invalid measured identity")
            if row["id"] in seen or (
                row["parent"] is not None and not isinstance(row["parent"], str)
            ):
                raise ValueError("Invalid measured relationship")
            seen.add(row["id"])
            for key, number in row.items():
                if key.endswith("_seconds") and (
                    type(number) not in (int, float) or not math.isfinite(number) or number < 0
                ):
                    raise ValueError("Invalid measured duration")
            if field == "spans":
                if row["status"] not in {"completed", "interrupted"}:
                    raise ValueError("Unsettled measured span")
            else:
                if not isinstance(row["operation"], str) or not row["operation"]:
                    raise ValueError("Invalid measured operation")
                if row.get("group") is not None and (
                    not isinstance(row["group"], str) or not row["group"]
                ):
                    raise ValueError("Invalid measured document group")
                for key in (
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "input_tokens",
                    "cache_miss_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                ):
                    if row.get(key) is not None and (type(row[key]) is not int or row[key] < 0):
                        raise ValueError("Invalid provider cache usage")
                for key in ("provider_model", "system_fingerprint"):
                    if row.get(key) is not None and not isinstance(row[key], str):
                        raise ValueError("Invalid provider model evidence")
                if "response_activity" in row:
                    from openkb.model_stream import validate_activity

                    validate_activity(row["response_activity"])
                if "effective_options" in row and not isinstance(row["effective_options"], dict):
                    raise ValueError("Invalid effective model options")
