"""Measure actual shared-use-case GC batches and reclaim lease intervals.

Uses synthetic data and the real SDK against a loopback HTTP provider. Requires
the source checkout, including tests. The output parent owns the temporary KB;
only that TemporaryDirectory is automatically removed.
"""

import argparse
import asyncio
import json
import logging
import math
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from ultrakb import config
from ultrakb.application.conversations import ask_question
from ultrakb.application.storage_maintenance import (
    backup_knowledge,
    collect_garbage,
    preview_collection,
    restore_knowledge,
)
from ultrakb.application.views import storage_scope
from ultrakb.locks import _Lease
from ultrakb.storage import maintenance
from ultrakb.storage.blobs import blob_path, put_blob
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import database_path
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.tree_store import TreeStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from stream_transport import StreamTransport  # noqa: E402
from test_storage_questions import question_knowledge  # noqa: E402


def timing(values):
    values = sorted(value * 1000 for value in values)
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        "median_ms": statistics.median(values),
        "p95_ms": values[math.ceil(len(values) * 0.95) - 1],
        "max_ms": max(values),
    }


class LeaseIntervals:
    """Observe outer OS reclaim acquisitions, preserving all real lock calls."""

    def __init__(self, root):
        self.path = (root / ".ultrakb/locks/reclaim.lock").resolve()
        self.phase = ContextVar("maintenance_calibration_phase", default="other")
        self.guard = threading.Lock()
        self.active = {}
        self.samples = []

    @contextmanager
    def during(self, phase):
        token = self.phase.set(phase)
        try:
            yield
        finally:
            self.phase.reset(token)

    def active_in(self, phase):
        with self.guard:
            return sum(item[1] == phase for item in self.active.values())

    def __enter__(self):
        acquire, release = _Lease.try_acquire, _Lease.release

        def measured_acquire(lease):
            acquired = acquire(lease)
            if acquired and lease.first and lease.path == self.path:
                with self.guard:
                    assert id(lease) not in self.active
                    self.active[id(lease)] = (
                        time.perf_counter(),
                        self.phase.get(),
                        lease.exclusive,
                    )
            return acquired

        def measured_release(lease):
            release(lease)
            if lease.path == self.path:
                ended = time.perf_counter()
                with self.guard:
                    sample = self.active.pop(id(lease), None)
                    if sample:
                        started, phase, exclusive = sample
                        self.samples.append((phase, exclusive, ended - started))

        self.original = acquire, release
        _Lease.try_acquire, _Lease.release = measured_acquire, measured_release
        return self

    def __exit__(self, *_):
        _Lease.try_acquire, _Lease.release = self.original
        assert not self.active, "An actual reclaim lease outlived the workload"

    def report(self):
        return {
            phase: {
                mode: timing(
                    elapsed
                    for item_phase, exclusive, elapsed in self.samples
                    if item_phase == phase and exclusive == (mode == "exclusive")
                )
                for mode in ("shared", "exclusive")
            }
            for phase in sorted({sample[0] for sample in self.samples})
        }


async def run_case(directory, nodes):
    root, identity, _ = question_knowledge(directory)
    scope = storage_scope(root, identity)
    bound = KnowledgeStore(root, identity).bind()
    with storage_stage(root, identity) as db:
        orphan = put_blob(db, b"Unrelated orphan, eligible during an actual HTTP model wait")
    busy_results = []

    def collect(phase, *, busy=False):
        with trace.during(phase):
            result = collect_garbage(root, scope=scope, max_items=1)
        if busy:
            assert result.status == "busy"
            assert result.deleted_count == result.trees_deleted == result.tree_rows_deleted == 0
            busy_results.append({"phase": phase, "status": result.status, "deleted": 0})
        return result

    with LeaseIntervals(root) as trace:
        entered, release = threading.Event(), threading.Event()
        original_read = Path.read_bytes
        target = blob_path(root, bound.file_digest("sources/fixture.md"))

        def gated_read(path):
            if path == target:
                entered.set()
                assert release.wait(10), "Actual read gate timed out"
            return original_read(path)

        Path.read_bytes = gated_read
        try:
            with trace.during("fixed_read_gated"):
                reader = asyncio.create_task(asyncio.to_thread(bound.read, "sources/fixture.md"))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                collect("gc_during_read", busy=True)
            finally:
                release.set()
                original = await asyncio.shield(reader)
            assert original == b"The value is 17."
        finally:
            Path.read_bytes = original_read

        entered, release = threading.Event(), threading.Event()
        original_copy = maintenance._copy_verified

        def gated_copy(source, destination, digest):
            if source.is_relative_to(root) and "blobs" in source.parts:
                entered.set()
                assert release.wait(10), "Actual backup copy gate timed out"
            return original_copy(source, destination, digest)

        maintenance._copy_verified = gated_copy
        try:
            with trace.during("backup_gated"):
                backup = asyncio.create_task(
                    asyncio.to_thread(backup_knowledge, root, directory / "backup", scope=scope)
                )
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                collect("gc_during_backup", busy=True)
            finally:
                release.set()
                backed_up = await asyncio.shield(backup)
            assert backed_up.status == "completed"
        finally:
            maintenance._copy_verified = original_copy
        restored = restore_knowledge(directory / "backup", directory / "restored")
        assert (
            KnowledgeStore(directory / "restored", restored.identity)
            .bind()
            .read("sources/fixture.md")
            == b"The value is 17."
        )

        gate = threading.Event()
        with StreamTransport([{"content": "The retained value is 17."}], gate=gate) as service:
            (root / ".env").write_text(
                f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n"
            )
            with trace.during("question"):
                question = asyncio.create_task(ask_question(root, "Read the value.", scope=scope))
            try:
                assert await asyncio.to_thread(service.wait_for_requests, 1)
                active_question_leases = trace.active_in("question")
                assert active_question_leases == 0
                collected = collect("gc_during_model_wait")
                assert collected.status == "completed" and collected.deleted_count == 1
                assert not blob_path(root, orphan.digest).exists()
            finally:
                gate.set()
                answer = await asyncio.shield(question)
            assert answer.status == "completed" and "17" in answer.answer
            sends = len(service.requests)
            assert sends == 1

        document = {
            "doc_name": "GC calibration",
            "structure": [
                {
                    "node_id": f"node-{ordinal}",
                    "title": f"Section {ordinal}",
                    "text": f"Original source row {ordinal}. " * 24,
                    "source_locator": {"kind": "block", "block_id": f"block-{ordinal}"},
                }
                for ordinal in range(nodes)
            ],
        }
        with trace.during("tree_build"):
            TreeStore(root, identity).build(
                document, protocol="document", input_fingerprint="c" * 64
            )
        with storage_stage(root, identity) as db:
            for ordinal in range(8):
                put_blob(db, f"Unreferenced blob {ordinal}".encode())
        batches = []
        for _ in range(1000):
            preview = preview_collection(root, scope=scope, max_items=1)
            if preview.candidate_count == preview.candidate_trees == 0:
                break
            started = time.perf_counter()
            result = collect("gc_bounded")
            batches.append(
                {
                    "seconds": time.perf_counter() - started,
                    "status": result.status,
                    "blobs": result.deleted_count,
                    "bytes": result.deleted_bytes,
                    "trees": result.trees_deleted,
                    "tree_rows": result.tree_rows_deleted,
                }
            )
        assert batches and len(batches) < 1000
        assert preview.candidate_count == preview.candidate_trees == 0
        assert all(batch["status"] == "completed" for batch in batches)
        assert bound.read("sources/fixture.md") == b"The value is 17."
        with storage_stage(root, identity, read_only=True) as db:
            assert db.connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        wal_bytes = Path(str(database_path(root)) + "-wal").stat().st_size

    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "nodes": nodes,
        "batch_limits": {"max_items": 1, "max_bytes": 64 * 1024**2},
        "busy_witnesses": busy_results,
        "model_wait": {"active_question_reclaim_leases": active_question_leases, "sends": sends},
        "lease_intervals": trace.report(),
        "gc": {
            "batches": len(batches),
            "batch_wall": timing(batch["seconds"] for batch in batches),
            "busy": sum(batch["status"] == "busy" for batch in batches),
            "blobs_deleted": sum(batch["blobs"] for batch in batches),
            "bytes_deleted": sum(batch["bytes"] for batch in batches),
            "trees_deleted": sum(batch["trees"] for batch in batches),
            "max_tree_rows_per_batch": max(batch["tree_rows"] for batch in batches),
            "remaining_candidates": 0,
        },
        "wal_bytes": wal_bytes,
        "integrity": "ok",
        "foreign_keys": "ok",
        "fixed_original_read": "unchanged",
        "backup_restored_original": "unchanged",
        "failures": [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=5000)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if not 1 <= options.nodes <= 20000:
        parser.error("nodes must be in 1..20000")
    output = options.output.resolve()
    if not output.parent.is_dir():
        parser.error("output parent must already exist")
    logging.disable(logging.INFO)
    with tempfile.TemporaryDirectory(prefix="maintenance-calibration-", dir=output.parent) as name:
        directory = Path(name)
        config.GLOBAL_CONFIG_DIR = directory / "settings"
        config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
        config.GLOBAL_CONFIG_LOCK_PATH = config.GLOBAL_CONFIG_DIR / "global.lock"
        report = asyncio.run(run_case(directory, options.nodes))
    output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
