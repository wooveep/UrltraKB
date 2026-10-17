"""Offline ledger-size comparison; never opens a real KB or sends a model request."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from ultrakb.kb_format import KB_FORMAT
from ultrakb.lifecycle import current_generation, read_lifecycle
from ultrakb.llm_usage import UsageRequest, UsageScope, aggregate_usage, finish_request
from ultrakb.locks import atomic_write_json
from ultrakb.usage_projection import progress_receipt


def compare(count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="ultrakb-usage-projection-") as name:
        kb = Path(name)
        requests = kb / ".ultrakb/usage/requests"
        requests.mkdir(parents=True)
        atomic_write_json(kb / ".ultrakb/format.json", KB_FORMAT)
        events = []
        with read_lifecycle(kb):
            scope = UsageScope(
                kb,
                "a" * 32,
                source_id="b" * 32,
                on_event=events.append,
                generation=current_generation(kb),
            )
        for number in range(count):
            record = UsageRequest(
                request_attempt_id=f"{number:032x}",
                execution_id=scope.execution_id,
                source_id=scope.source_id,
                model="offline",
                stage="summary:window",
                started_at="2026-10-18T00:00:00+00:00",
                ended_at="2026-10-18T00:00:01+00:00",
                state="completed",
                observation="transport",
                send_status="sent",
                input_total=17,
                output_total=3,
                cached_input=2,
                reasoning_output=1,
                usage_status="known",
            )
            (requests / f"{record.request_attempt_id}.json").write_text(record.model_dump_json())
        started = time.monotonic()
        progress_receipt(kb, execution_ids=[scope.execution_id], source_ids=[scope.source_id])
        rebuild_seconds = time.monotonic() - started
        reads = []
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path.parent == requests:
                reads.append(path.stat().st_size)
            return original(path, *args, **kwargs)

        elapsed = []
        with patch.object(Path, "read_text", read):
            for _ in range(10):
                started = time.monotonic()
                finish_request(f"{count - 1:032x}", scope=scope)
                elapsed.append(time.monotonic() - started)
        live = events[-1]["data"]["cumulative"]
        audit = aggregate_usage(kb, source_id=scope.source_id)
        assert live["requests"] == count and live["input_total"] == 17 * count
        for key in (
            "input_total",
            "output_total",
            "cached_input",
            "reasoning_output",
            "actual_sends",
            "unknown_fields",
            "collection_complete",
            "usage_complete",
        ):
            assert live[key] == audit[key], key
        return {
            "ledger_requests": count,
            "cold_rebuild_seconds": rebuild_seconds,
            "warm_callback_median_ms": statistics.median(elapsed) * 1000,
            "ledger_files_read_per_callback": len(reads) / 10,
            "ledger_bytes_read_per_callback": sum(reads) / 10,
            "event_bytes": len(json.dumps(events[-1]).encode()),
            "provider_sends": 0,
            "audit_matches": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    atomic_write_json(options.output, {"comparisons": [compare(n) for n in (1000, 5000, 20000)]})


if __name__ == "__main__":
    main()
