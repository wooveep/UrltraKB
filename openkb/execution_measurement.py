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


class Measurement:
    def __init__(self):
        self.started = time.monotonic()
        self.identity = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.active_requests = 0
        self.analysis_events = set()
        self.value: dict[str, Any] = {
            "schema": 1,
            "spans": [],
            "requests": [],
            "peak_concurrency": 0,
            "analyses": [],
        }

    def begin_request(self, observation, queue_seconds, preparation_seconds, *, options=None):
        with self.lock:
            row = {
                "id": f"{self.identity}:{observation['attempt']}",
                "stage": observation["stage"],
                "operation": _OPERATION.get() or observation["stage"],
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
                "provider_model": None,
                "system_fingerprint": None,
                "effective_options": effective_options(options or {}),
            }
            self.value["requests"].append(row)
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


def measurement_identity():
    measurement = _ACTIVE.get()
    return measurement.identity if measurement else None


def validate_measurement(value):
    if (
        not isinstance(value, dict)
        or set(value)
        not in (
            {"schema", "spans", "requests", "peak_concurrency"},
            {"schema", "spans", "requests", "peak_concurrency", "analyses"},
        )
        or value["schema"] != 1
        or type(value["schema"]) is not int
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
                "provider_model",
                "system_fingerprint",
                "effective_options",
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
                if "effective_options" in row and not isinstance(row["effective_options"], dict):
                    raise ValueError("Invalid effective model options")
