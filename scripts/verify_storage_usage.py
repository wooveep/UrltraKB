"""Measure real storage callbacks/audits over synthetic, explicitly unsent workload data."""

import argparse
import json
import math
import platform
import sqlite3
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_tasks import list_knowledge_tasks
from ultrakb.config import save_config
from ultrakb.llm_usage import UsageRequest
from ultrakb.locks import atomic_write_text
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.costs import capture_costs
from ultrakb.storage.database import SCHEMA_VERSION, create_database, database_path
from ultrakb.storage.execution_store import ExecutionStore, now
from ultrakb.storage.usage_audit import UsageAudit
from ultrakb.storage.usage_store import UsageStore


def distribution(values):
    ordered = sorted(values)
    return {
        "samples": len(values),
        "median_seconds": statistics.median(values),
        "p95_seconds": ordered[math.ceil(0.95 * len(values)) - 1],
        "maximum_seconds": max(values),
    }


def measured(operation, observations):
    began = perf_counter()
    with capture_costs() as capture:
        value = operation()
    elapsed, costs = perf_counter() - began, capture.finish()
    assert costs.writer_attempts == costs.writer_commits == 1
    assert costs.writer_errors == costs.writer_rollbacks == 0
    observations.append((elapsed, costs.writer_wait_seconds, costs.writer_hold_seconds))
    return value


def run(directory, checkpoints):
    root = directory / "knowledge"
    root.mkdir()
    identity = create_database(root)
    catalog = CatalogStore(root, identity)
    units = []
    for index in range(10):
        source = directory / f"source-{index}.md"
        source.write_text(f"Synthetic source {index}.\n")
        original = catalog.admit(source, source_key=f"source-{index}")
        processing = catalog.retain_processing(
            original.revision_id,
            {source.name: source},
            body_path=source.name,
            source_path=source.name,
            source_kind="markdown",
            fingerprint="usage-calibration-v1",
            operation_id=f"calibration-{index}",
            decision=classify_markdown_tokens(10),
        )
        units.append(catalog.plan_unit(processing.processing_id, key="body", doc_name=source.stem))
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 1, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    atomic_write_text(
        root / ".env",
        "LLM_API_KEY=offline-test-key\nOPENAI_API_BASE=http://127.0.0.1:9/v1\n",
    )
    context = ExecutionContext(storage_identity=identity)
    with context.begin(root):
        return run_callbacks(root, identity, units, checkpoints, context.storage_execution)


def run_callbacks(root, identity, units, checkpoints, execution):
    executions = ExecutionStore(root, identity)
    usage, observations, results = UsageStore(root, identity), [], []
    for number in range(1, max(checkpoints) + 1):
        unit = units[number % len(units)]
        record = UsageRequest(
            request_attempt_id=uuid4().hex,
            execution_id=execution.execution_id,
            logical_call_id=uuid4().hex,
            model="synthetic-storage-calibration",
            stage="calibration",
            started_at=now(),
            source_id=unit.source_id,
            source_revision_id=unit.source_revision_id,
            unit_id=unit.unit_id,
            unit_revision_id=unit.unit_revision_id,
        )
        measured(
            lambda: usage.begin(
                record,
                model_budget_id=execution.model_budget_id,
                stage_code=f"calibration.{number % 4}.v1",
            ),
            observations,
        )
        # Synthetic known-unsent outcomes exercise the actual receipt/counter
        # transaction without claiming provider traffic, usage or billing.
        measured(
            lambda: usage.merge(
                record.request_attempt_id,
                version=1,
                state="failed",
                send_status="not_sent",
                ended_at=now(),
                input_total=0,
                output_total=0,
                cached_input=0,
                reasoning_output=0,
                observation="transport",
            ),
            observations,
        )
        if number in checkpoints:
            timings = {"task_list": [], "source_summary": [], "full_audit": []}
            for _ in range(7):
                began = perf_counter()
                list_knowledge_tasks(root, identity=identity)
                timings["task_list"].append(perf_counter() - began)
                began = perf_counter()
                total = usage.summary(source_id=units[0].source_id)
                timings["source_summary"].append(perf_counter() - began)
                assert total["actual_sends"] == 0
                began = perf_counter()
                audit = UsageAudit(root, identity).run()
                timings["full_audit"].append(perf_counter() - began)
                assert audit.status == "consistent" and audit.request_count == number
            # Exclude the first 50 request pairs from callback latency summaries.
            samples = observations[100:]
            results.append(
                {
                    "requests": number,
                    "actual_network_sends": 0,
                    "callbacks": distribution([value[0] for value in samples]),
                    "writer_wait": distribution([value[1] for value in samples]),
                    "writer_hold": distribution([value[2] for value in samples]),
                    "queries": {key: distribution(value) for key, value in timings.items()},
                    "wal_bytes": Path(str(database_path(root)) + "-wal").stat().st_size,
                    "database_bytes": database_path(root).stat().st_size,
                    "counter_rows": audit.counter_count,
                    "budget_used_after_known_unsent": executions.budget(
                        execution.model_budget_id
                    ).used,
                }
            )
    return {
        "schema_version": 1,
        "storage_schema": SCHEMA_VERSION,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "workload": "Synthetic known-unsent requests in an actual shared application execution",
        "sources_and_units": 10,
        "stage_codes": 4,
        "excluded_warmup_pairs": 50,
        "network_model_calls": 0,
        "interpretation": "One host, storage boundary only; no SDK/network throughput claim.",
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoints", type=int, nargs="+", default=[1000, 5000, 20000])
    args = parser.parse_args()
    if args.checkpoints != sorted(set(args.checkpoints)) or not all(
        50 < value <= 100000 for value in args.checkpoints
    ):
        parser.error("--checkpoints must be increasing unique request counts between 51 and 100000")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="usage-", dir=args.output.parent) as temporary:
        result = run(Path(temporary), args.checkpoints)
    atomic_write_text(args.output, json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
