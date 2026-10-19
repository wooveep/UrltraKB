"""Compare optional observations over real XLSX parsing with core ledgers enabled."""

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter

import openpyxl

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import read_knowledge_task
from ultrakb.application.storage_execution import begin_storage_operation
from ultrakb.application.task_stages import task_stage
from ultrakb.locks import atomic_write_text
from ultrakb.storage.database import create_database
from ultrakb.workbooks.xlsx import read_xlsx


def sample(root, identity, source, enabled):
    context = ExecutionContext(storage_identity=identity, observe_storage_costs=enabled)
    began = perf_counter()
    with begin_storage_operation(root, context, "observation_calibration"):
        with task_stage(context, "worksheet.parse.v1", root=root):
            sheets = read_xlsx(source)
        context._storage_result = "completed"
    elapsed = perf_counter() - began
    # Validate real outputs and core persistence outside the timed interval.
    encoded = b"\n".join(sheet.model_dump_json().encode() for sheet in sheets)
    detail = read_knowledge_task(root, context.storage_execution.task_id, identity=identity)
    assert detail.execution.state == "completed" and len(detail.stages) == 1
    assert detail.task.budget.used == detail.task.budget.max_calls == 0
    assert not detail.requests
    assert detail.stages[0].observation_coverage == ("recorded" if enabled else "not_recorded")
    assert len(sheets) == 5 and all(sheet.error is None for sheet in sheets)
    return {
        "enabled": enabled,
        "seconds": elapsed,
        "output_digest": hashlib.sha256(encoded).hexdigest(),
        "output_bytes": len(encoded),
        "cells": sum(len(sheet.cells) for sheet in sheets),
    }


def distribution(values):
    ordered = sorted(values)
    return {
        "samples": len(values),
        "median_seconds": statistics.median(values),
        "p95_seconds": ordered[math.ceil(0.95 * len(values)) - 1],
        "maximum_seconds": max(values),
    }


def run(directory, pairs):
    root = directory / "knowledge"
    root.mkdir()
    identity = create_database(root)
    source = directory / "five-sheets.xlsx"
    workbook = openpyxl.Workbook()
    for sheet_number in range(5):
        sheet = workbook.active if sheet_number == 0 else workbook.create_sheet()
        sheet.title = f"Dataset {sheet_number + 1}"
        for row in range(1, 1001):
            sheet.append([f"Record {row}", row, row / 7, date(2026, 1, 1), f"=B{row}*2"])
    workbook.save(source)
    records = []
    for pair in range(pairs + 1):
        current = []
        for enabled in (False, True) if pair % 2 == 0 else (True, False):
            current.append(sample(root, identity, source, enabled))
        if pair:
            records.extend(current)
    assert len({record["output_digest"] for record in records}) == 1
    assert {record["cells"] for record in records} == {25000}
    disabled = distribution([record["seconds"] for record in records if not record["enabled"]])
    enabled = distribution([record["seconds"] for record in records if record["enabled"]])
    difference = enabled["median_seconds"] - disabled["median_seconds"]
    overhead = difference / disabled["median_seconds"] * 100
    return {
        "schema_version": 1,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "openpyxl": openpyxl.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "workload": {
            "sheets": 5,
            "rows_per_sheet": 1000,
            "cells": 25000,
            "input_bytes": source.stat().st_size,
            "core_ledger": "always_enabled",
            "timed_scope": "execution_and_stage_commits_plus_actual_xlsx_parsing",
            "warmup_pairs": 1,
            "measured_pairs": pairs,
        },
        "disabled": disabled,
        "enabled": enabled,
        "median_overhead_seconds": difference,
        "median_overhead_percent": overhead,
        "initial_target_percent": 5,
        "within_initial_target": overhead <= 5,
        "interpretation": "One host/workload; signed negative differences are measurement noise.",
        "samples": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=7)
    args = parser.parse_args()
    if not 3 <= args.pairs <= 100:
        parser.error("--pairs must be between 3 and 100")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="observations-", dir=args.output.parent) as temporary:
        result = run(Path(temporary), args.pairs)
    encoded = json.dumps(result, indent=2) + "\n"
    atomic_write_text(args.output, encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
