"""Explicit phase analysis; overlapping requests never become summed wall time."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ultrakb.execution_timing import PhaseEvent, read_phase_events


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged)


def _metrics(events: list[PhaseEvent]) -> dict:
    sends = [row for row in events if row.event == "send"]
    responses = [
        row
        for row in events
        if row.event in {"response", "request_settled"} and row.duration_ns is not None
    ]
    clock_ids = {row.clock_id for row in events}
    one_clock = len(clock_ids) == 1
    intervals = []
    for row in responses:
        assert row.started_monotonic_ns is not None and row.started_at is not None
        intervals.append(
            (row.started_monotonic_ns / 1e9, row.monotonic_ns / 1e9)
            if one_clock
            else (
                datetime.fromisoformat(row.started_at).timestamp(),
                datetime.fromisoformat(row.occurred_at).timestamp(),
            )
        )
    points = [
        (
            row.monotonic_ns / 1e9
            if one_clock
            else datetime.fromisoformat(row.occurred_at).timestamp(),
            1 if row.event == "send" else -1,
        )
        for row in [*sends, *responses]
    ]
    current = peak = 0
    for _, delta in sorted(points, key=lambda point: (point[0], point[1])):
        current += delta
        peak = max(peak, current)
    utc_points = [
        datetime.fromisoformat(value).timestamp()
        for row in events
        for value in (row.occurred_at, row.started_at)
        if value is not None
    ]
    monotonic_points = [
        value / 1e9
        for row in events
        for value in (row.monotonic_ns, row.started_monotonic_ns)
        if value is not None
    ]
    wall_points = monotonic_points if one_clock else utc_points
    wall = max(wall_points) - min(wall_points) if wall_points else None
    union = _union(intervals) if responses else None
    slot_limits = {row.slot_limit for row in events if row.slot_limit is not None}
    slot_limit = next(iter(slot_limits)) if len(slot_limits) == 1 else None
    held = sum((row.duration_ns or 0) / 1e9 for row in events if row.event == "slot_released")
    durations: dict[str, float] = defaultdict(float)
    for row in events:
        if row.duration_ns is not None:
            durations[row.event] += row.duration_ns / 1e9
    return {
        "observed_events": len(events),
        "events_by_type": dict(Counter(row.event for row in events)),
        "actual_sends": len(sends),
        "settled_requests": len(responses),
        "observed_responses": sum(row.event == "response" for row in responses),
        "in_flight_requests": len(
            {row.request_attempt_id for row in sends}
            - {row.request_attempt_id for row in responses}
        ),
        "peak_in_flight_requests": peak if sends else None,
        "stage_wall_clock_seconds": wall,
        "request_interval_union_seconds": union,
        "concurrent_request_seconds_sum": sum((row.duration_ns or 0) / 1e9 for row in responses)
        if responses
        else None,
        "interval_clock": "monotonic" if one_clock else "utc_cross_process" if events else None,
        "queue_wait_seconds_sum": sum(
            (row.duration_ns or 0) / 1e9 for row in events if row.event == "slot_acquired"
        ),
        "observed_duration_seconds_by_event": dict(durations),
        "model_slot_limit": slot_limit,
        "model_slot_held_seconds_sum": held,
        "model_slot_utilization": held / (wall * slot_limit) if wall and slot_limit else None,
        "first_event_beijing": datetime.fromtimestamp(
            min(utc_points), ZoneInfo("Asia/Shanghai")
        ).isoformat()
        if utc_points
        else None,
        "last_event_beijing": datetime.fromtimestamp(
            max(utc_points), ZoneInfo("Asia/Shanghai")
        ).isoformat()
        if utc_points
        else None,
    }


def build_timing_report(kb_dir: Path, *, execution_ids=None, source_id=None) -> dict:
    rows = [
        row
        for row in read_phase_events(kb_dir, execution_ids=execution_ids)
        if source_id is None or row.source_id == source_id
    ]
    stages = defaultdict(list)
    executions: dict[str, list[PhaseEvent]] = defaultdict(list)
    tasks = {}
    for row in rows:
        stages[row.stage.split(":", 1)[0]].append(row)
        executions[row.execution_id].append(row)
        if row.execution_id not in tasks:
            # Resolve task association once per execution, never per hot-path event.
            from ultrakb.llm_usage_models import UsageExecution

            path = kb_dir / ".ultrakb/usage/executions" / f"{row.execution_id}.json"
            tasks[row.execution_id] = (
                UsageExecution.model_validate_json(path.read_bytes()).task_id
                if path.exists()
                else row.task_id
            )
    return {
        "schema_version": 1,
        "observation_status": "recorded" if rows else "unrecorded",
        "history_limitation": (
            "Only retained observations are measurable; historical conversion, OCR and GUI "
            "timings are unavailable without recorded events."
        ),
        **_metrics(rows),
        "execution_tasks": tasks,
        "executions": {identity: _metrics(items) for identity, items in sorted(executions.items())},
        "stages": {stage: _metrics(items) for stage, items in sorted(stages.items())},
    }
