"""Small paid comparison with identical source, requests and model settings."""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from live_llm import LiveLLM

from ultrakb.agent.document_summary import (
    SYSTEM_TEMPLATE,
    build_document_summary,
    summary_request_protocol,
)
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.compile_checkpoints import digest
from ultrakb.locks import atomic_write_json, atomic_write_text


class _RssSampler:
    """Current Linux process RSS, sampled independently from the asyncio loop."""

    def __init__(self):
        self.samples = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _read(self):
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        self.samples.append(resident_pages * os.sysconf("SC_PAGE_SIZE"))

    def _sample(self):
        while not self.stop.wait(0.01):
            self._read()

    def __enter__(self):
        self._read()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        self._read()

    def report(self):
        return {
            "baseline_bytes": self.samples[0],
            "peak_bytes": max(self.samples),
            "peak_increase_bytes": max(self.samples) - self.samples[0],
            "samples": len(self.samples),
            "method": (
                "Linux /proc/self/statm resident pages * SC_PAGE_SIZE, sampled every 10 ms "
                "plus arm boundaries. Includes this pytest process, SDK and loopback gateway "
                "threads; excludes external provider memory. Sampling can miss sub-10-ms peaks."
            ),
        }


def _phase_metrics(events, wall_seconds, concurrency):
    waits = [(row.duration_ns or 0) / 1e9 for row in events if row.event == "slot_acquired"]
    held = sum((row.duration_ns or 0) / 1e9 for row in events if row.event == "slot_released")
    return {
        "events": len(events),
        "queued": sum(row.event == "queued" for row in events),
        "slots_acquired": len(waits),
        "slots_released": sum(row.event == "slot_released" for row in events),
        "queue_wait_seconds_sum": sum(waits),
        "queue_wait_seconds_max": max(waits, default=None),
        "slot_held_seconds_sum": held,
        "shared_model_slots": 3,
        "shared_model_slot_utilization": held / (wall_seconds * 3),
        "effective_stage_slot_utilization": held / (wall_seconds * concurrency),
    }


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_summary_serial_and_parallel_comparison(kb_dir, tmp_path, live_llm, monkeypatch):
    from ultrakb.execution_timing import read_phase_events, timing_report
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelCallPolicy,
        RoleBindings,
        active_executor,
    )
    from ultrakb.llm_usage import active_scope, aggregate_usage, read_requests
    from ultrakb.llm_usage_execution import bind_model_task, import_usage_execution
    from ultrakb.model_task import TaskJournal

    original = (
        "Close the inlet before maintenance. Reopen it only after pressure reaches zero.\n\n"
        "During inspection keep the vent closed, "
        "except when an authorized purge requires it open.\n\n"
        "Replace a leaking seal. If no leak is present, leave the seal installed."
    )
    atomic_write_text(
        kb_dir / "wiki/sources/manual.md",
        original,
    )
    atomic_write_text(
        kb_dir / "wiki/summaries/manual.md", "---\nfull_text: sources/manual.md\n---\nOld."
    )
    windows = CompileSourceWindows(kb_dir / "wiki", "manual", navigation=[])
    results = []
    signatures = []
    client = LiveLLM(live_llm.budget, response_cache=tmp_path / "arm-1.json")
    client.install(monkeypatch)
    executor = journal = scope = None
    try:
        with import_usage_execution(kb_dir, "summary_concurrency_comparison") as scope:
            executor = CompletionExecutor(
                RoleBindings(
                    "deepseek/deepseek-flash",
                    client.credentials,
                    generation={"compile": {"max_tokens": 512, "temperature": 0}},
                ),
                ModelCallPolicy(max_calls=18, concurrency=3, retries=1, backoff=0.2),
                scope=scope,
            )
            journal = TaskJournal(kb_dir, executor)
            bind_model_task(journal.record.task_id)
            with executor.activate():
                for concurrency in (1, 2, 3):
                    # The test gateway's response store is arm-specific. One frozen
                    # executor, scope, model budget and credential binding persist.
                    client._cache_path = tmp_path / f"arm-{concurrency}.json"
                    requests = []
                    decoded_windows = {}
                    receipt_start, timing_start = len(client.receipts), len(client.timings)
                    errors_start, budget_start = len(executor.model_errors), executor.calls
                    provider_start = live_llm.budget.report()["provider_requests"]
                    prior_events = {row.event_id for row in read_phase_events(kb_dir)}
                    prior_requests = {row.request_attempt_id for row in read_requests(kb_dir)}

                    async def generate(messages, stage, originals, sections):
                        from ultrakb.agent.compiler import _llm_call_page_async
                        from ultrakb.agent.page_response import parse_summary
                        from ultrakb.agent.source_excerpts import SourceExcerpts

                        assert active_executor() is executor and active_scope() is scope
                        requests.append((stage, digest(messages)))
                        raw = await _llm_call_page_async(
                            "deepseek/deepseek-flash",
                            messages,
                            stage,
                            response_format={"type": "json_object"},
                            max_tokens=512,
                            temperature=0,
                            original_excerpts=SourceExcerpts(originals),
                            response_protocol=summary_request_protocol(messages),
                            expected_sections=sections,
                        )
                        decoded = parse_summary(raw, sections)
                        decoded_windows[stage] = decoded
                        return decoded

                    with _RssSampler() as rss:
                        started = time.monotonic()
                        draft = await build_document_summary(
                            windows,
                            system_message={
                                "role": "system",
                                "content": SYSTEM_TEMPLATE.format(language="en"),
                            },
                            generate=generate,
                            fits=lambda messages: len(
                                json.loads(messages[1]["content"].split("\n", 1)[1])["content"]
                            )
                            <= 110,
                            max_concurrency=concurrency,
                        )
                        wall_seconds = time.monotonic() - started
                    timings = client.timings[timing_start:]
                    active = peak = 0
                    for _, delta in sorted(
                        [(row["start"], 1) for row in timings]
                        + [(row["end"], -1) for row in timings]
                    ):
                        active += delta
                        peak = max(peak, active)
                    events = [
                        row for row in read_phase_events(kb_dir) if row.event_id not in prior_events
                    ]
                    records = [
                        row
                        for row in read_requests(kb_dir)
                        if row.request_attempt_id not in prior_requests
                    ]
                    request_ids = [row.request_attempt_id for row in records]
                    logical_calls = {row.logical_call_id for row in records}
                    errors = executor.model_errors[errors_start:]
                    provider_sends = live_llm.budget.report()["provider_requests"] - provider_start
                    results.append(
                        {
                            "concurrency": concurrency,
                            "wall_seconds": wall_seconds,
                            "provider_sends": provider_sends,
                            "peak_provider_in_flight": peak,
                            "usage": [row["usage"] for row in client.receipts[receipt_start:]],
                            "provider_wire_fingerprints": sorted(
                                row["fingerprint"] for row in client.receipts[receipt_start:]
                            ),
                            "ledger_usage": aggregate_usage(kb_dir, request_ids=request_ids),
                            "transport_retry_attempts": len(records) - len(logical_calls),
                            "extra_logical_calls": len(logical_calls) - 3,
                            "observed_429s": sum(
                                getattr(error, "status_code", None) == 429 for error in errors
                            ),
                            "model_errors": [type(error).__name__ for error in errors],
                            "shared_budget_before": budget_start,
                            "shared_budget_after": executor.calls,
                            "rss": rss.report(),
                            "phase_timing": _phase_metrics(events, wall_seconds, concurrency),
                            "phase_events": [row.model_dump(mode="json") for row in events],
                            "markdown": draft.markdown,
                            "windows": decoded_windows,
                            "ranges": list(draft.ranges),
                        }
                    )
                    signatures.append([value for _, value in sorted(requests)])
                    assert provider_sends == len(client.receipts[receipt_start:]) == 3
                    assert peak == concurrency and len(draft.ranges) == 3
                    assert executor.calls - budget_start == 3
                    assert [row["selection_offset"] for row in draft.ranges] == sorted(
                        row["selection_offset"] for row in draft.ranges
                    )
        assert signatures[0] == signatures[1] == signatures[2]
        assert (
            results[0]["provider_wire_fingerprints"]
            == results[1]["provider_wire_fingerprints"]
            == results[2]["provider_wire_fingerprints"]
        )
    finally:
        client.close()
        atomic_write_json(
            live_llm.budget.path.with_suffix(".summary-comparison.json"),
            {
                "arms": results,
                "cost": live_llm.budget.report(),
                "original": original,
                "request_digests_by_arm": signatures,
                "shared_scope_execution_id": scope.execution_id if scope else None,
                "shared_model_task_id": journal.record.task_id if journal else None,
                "shared_executor_fingerprint": executor.fingerprint if executor else None,
                "shared_model_slots": 3,
                "model_options": {"max_tokens": 512, "temperature": 0},
                "cumulative_phase_report": timing_report(kb_dir),
                "interpretation": (
                    "One sequential 1/2/3-arm sample with identical request bytes and "
                    "separate real-response stores. Provider load, cache warmth and "
                    "one-process warmup can affect the observed comparison. "
                    "Concurrency 3 is evaluated only; production default remains 2."
                ),
            },
        )
