"""Calibrate bounded shared-tree writes against actual receipt and reader traffic.

Synthetic inputs only; no model calls. Run with the project's Python runtime:
    .venv/bin/python -m scripts.verify_shared_storage --nodes 5000
"""

import json
import math
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ultrakb.llm_usage import UsageRequest
from ultrakb.storage.database import Database, create_database, database_path
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.maintenance import Maintenance
from ultrakb.storage.tree_store import TreeStore
from ultrakb.storage.usage_store import UsageStore


def run_case(node_count=5000, directory=None):
    samples = {}
    failures = []
    callback_times = []
    overlapping_callbacks = []
    original = Database.transaction

    @contextmanager
    def measured(self):
        start = time.perf_counter()
        with original(self) as connection:
            entered = time.perf_counter()
            yield connection
        ended = time.perf_counter()
        samples.setdefault(threading.current_thread().name, []).append(
            (entered - start, ended - entered)
        )

    Database.transaction = measured

    try:

        def stats(values):
            values = sorted(v * 1000 for v in values)
            return (
                {
                    "samples": len(values),
                    "p95_ms": values[math.ceil(0.95 * len(values)) - 1],
                    "max_ms": max(values),
                    "median_ms": statistics.median(values),
                }
                if values
                else {}
            )

        with tempfile.TemporaryDirectory(
            prefix=".ukb-164-tree-calibration-", dir=directory
        ) as name:
            root = Path(name) / "kb"
            identity = create_database(root)
            execution = ExecutionStore(root, identity).begin(
                "calibration", fingerprint="a" * 64, max_calls=None
            )
            usage = UsageStore(root, identity)
            request = UsageRequest(
                request_attempt_id=uuid4().hex,
                execution_id=execution.execution_id,
                model="calibration/no-model",
                stage="calibration.receipt",
                started_at="2026-10-18T00:00:00+00:00",
                observation="transport",
            )
            usage.begin(
                request, model_budget_id=execution.model_budget_id, stage_code="calibration.receipt"
            )
            trees = TreeStore(root, identity)
            anchor = trees.build(
                {"content": "fixed reader anchor"}, protocol="generic", input_fingerprint="b" * 64
            )
            start = threading.Barrier(3)
            stop = threading.Event()

            document = {
                "doc_name": "calibration document",
                "structure": [
                    {
                        "node_id": f"node-{i}",
                        "title": f"Section {i}",
                        "text": f"Original source row {i}. " * 24,
                        "source_locator": {"kind": "block", "block_id": f"block-{i}"},
                    }
                    for i in range(node_count)
                ],
            }
            build_result = {}

            def build():
                try:
                    start.wait()
                    t = time.perf_counter()
                    result = trees.build(document, protocol="document", input_fingerprint="c" * 64)
                    build_result.update(tree_id=result.tree_id, seconds=time.perf_counter() - t)
                    with trees.open(result.tree_id) as bound:
                        assert bound.content(bound.root_id)["document"] == document
                except BaseException as error:
                    failures.append(repr(error))
                finally:
                    stop.set()

            def receipts():
                try:
                    start.wait()
                    for version in range(1, 501):
                        t = time.perf_counter()
                        overlaps = not stop.is_set()
                        usage.merge(
                            request.request_attempt_id,
                            version=version,
                            send_status="sent",
                            input_total=version,
                            output_total=version // 2,
                        )
                        callback_times.append(time.perf_counter() - t)
                        if overlaps:
                            overlapping_callbacks.append(callback_times[-1])
                        if version % 10 == 0:
                            time.sleep(0.002)
                except BaseException as error:
                    failures.append(repr(error))

            read_times = []

            def reads():
                try:
                    with trees.open(anchor.tree_id) as bound:
                        start.wait()
                        while not stop.is_set():
                            t = time.perf_counter()
                            assert bound.content(bound.root_id)["content"] == "fixed reader anchor"
                            usage.summary(execution_id=execution.execution_id)
                            read_times.append(time.perf_counter() - t)
                            time.sleep(0.002)
                except BaseException as error:
                    failures.append(repr(error))

            workers = [
                threading.Thread(target=build, name="tree-build"),
                threading.Thread(target=receipts, name="receipt"),
                threading.Thread(target=reads, name="reader"),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            wal_bytes = Path(str(database_path(root)) + "-wal").stat().st_size
            maintenance = Maintenance(root, identity)
            gc_results = []
            for _ in range(1000):
                if not maintenance.preview(max_items=1).candidate_trees:
                    break
                t = time.perf_counter()
                result = maintenance.collect(max_items=1)
                gc_results.append(
                    {
                        "seconds": time.perf_counter() - t,
                        "rows": result.tree_rows_deleted,
                        "trees": result.trees_deleted,
                        "status": result.status,
                    }
                )
            report = {
                "python": sys.version,
                "sqlite": sqlite3.sqlite_version,
                "nodes": node_count,
                "build": build_result,
                "failures": failures,
                "wal_bytes": wal_bytes,
                "callbacks": stats(callback_times),
                "callbacks_started_during_build": stats(overlapping_callbacks),
                "reads": stats(read_times),
                "transactions": {
                    key: {"wait": stats(v[0] for v in values), "held": stats(v[1] for v in values)}
                    for key, values in samples.items()
                },
                "gc": {
                    "batches": len(gc_results),
                    "max_rows": max(x["rows"] for x in gc_results),
                    "busy": sum(x["status"] == "busy" for x in gc_results),
                    "timing": stats(x["seconds"] for x in gc_results),
                    "remaining_trees": maintenance.preview(max_items=1).candidate_trees,
                },
            }
            assert not failures, failures
            return report
    finally:
        Database.transaction = original


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=5000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--directory", type=Path, help="Parent filesystem for the temporary new KB")
    options = parser.parse_args()
    if not 1 <= options.nodes <= 20000:
        parser.error("nodes must be in 1..20000")
    report = run_case(options.nodes, options.directory)
    encoded = json.dumps(report, indent=2) + "\n"
    if options.output:
        options.output.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
