"""One document can generate independent planned pages concurrently without racing publication."""

import json
import threading
import time

import litellm
import pytest
import yaml

from openkb.agent.compilation_index import checkpoint_keys
from openkb.application.documents import import_document
from openkb.sources import SourceStore
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


def _parallel_page_plan(payload):
    target = payload["target"]
    ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
    page_ranges = []
    for value in ranges:
        if isinstance(value, dict):
            page_ranges.append([value])
            continue
        start, end = value
        page_ranges.extend([[[index, min(index + 4, end)]] for index in range(start, end, 4)])

    def name(value):
        first = value[0]
        if isinstance(first, dict):
            return "-".join(
                str(first[field]) for field in ("block_index", "start_char", "end_char")
            )
        return f"{first[0]}-{first[1]}"

    return {
        "overview": {
            "text": "Independent pressure conditions.",
            "ranges": ranges,
            "limitations": [],
        },
        "page_changes": [
            {
                "local_key": f"conditions-{name(page_range)}",
                "target_key": "",
                "target": "",
                "kind": "concept",
                "name": f"concepts/conditions-{name(page_range)}",
                "title": "Pressure conditions " + name(page_range),
                "purpose": "Record the supplied pressure conditions.",
                "subject_ranges": page_range,
                "necessary_context": [],
            }
            for page_range in page_ranges
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }


@pytest.mark.parametrize("concurrency", [1, 2, 4, 8])
def test_single_document_uses_bounded_parallel_batches_and_keeps_all_checkpoints(
    kb_dir, tmp_path, monkeypatch, concurrency
):
    path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["processing"].update(
        context_tokens=16384,
        output_tokens=1024,
        concurrency=concurrency,
        max_requests=500,
    )
    path.write_text(yaml.safe_dump(settings))
    source = tmp_path / "parallel.md"
    source.write_text(
        "\n\n".join(f"Condition {i}: pressure must remain below 37 kPa." for i in range(64))
    )
    concurrency = min(concurrency, 4)
    guard = threading.Lock()
    overlap = threading.Event()
    active = peak = 0
    inputs = []

    def completion(**kwargs):
        nonlocal active, peak
        payload = json.loads(kwargs["messages"][-1]["content"])
        if payload["stage"] == "planning":
            return response(_parallel_page_plan(payload))
        if payload["stage"] == "generation":
            with guard:
                active += 1
                peak = max(peak, active)
                inputs.extend(payload["evidence"]["blocks"])
                if active == concurrency:
                    overlap.set()
            assert overlap.wait(3), "The configured page workers did not overlap"
            time.sleep(0.01)
            with guard:
                active -= 1
        return response(evidence_response(payload))

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert peak == concurrency
    # Request-local occurrence IDs are compact; original page evidence proves full coverage.
    assert len(inputs) == len({block["text"] for block in inputs}) == 64
    root = kb_dir / ".openkb/source-store/compilation"
    saved = {path.stem for path in root.glob("*.json")}
    assert set(checkpoint_keys(SourceStore(kb_dir), result.input_version) or ()) == saved


@pytest.mark.parametrize("concurrency", [2, 8])
def test_stop_cancels_all_inflight_batches_without_publishing(
    kb_dir, tmp_path, monkeypatch, concurrency
):
    from openkb.cancellation import cancellation_scope

    path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["processing"].update(context_tokens=16384, output_tokens=1024, concurrency=concurrency)
    path.write_text(yaml.safe_dump(settings))
    source = tmp_path / "cancel-parallel.md"
    source.write_text("\n\n".join(f"Condition {i}: mandatory requirement." for i in range(64)))
    all_started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    concurrency = min(concurrency, 4)
    guard = threading.Lock()
    calls = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        if payload["stage"] == "planning":
            return response(_parallel_page_plan(payload))
        assert payload["stage"] == "generation"
        with guard:
            calls.append(kwargs)
            if len(calls) == concurrency:
                all_started.set()
        release.wait(10)
        return response({})

    stopped_at = []

    def cancel():
        all_started.wait(3)
        stopped_at.append(time.monotonic())
        stopped.set()

    monkeypatch.setattr(litellm, "completion", completion)
    canceller = threading.Thread(target=cancel)
    canceller.start()
    try:
        with cancellation_scope(stopped.is_set):
            result = import_document(kb_dir, source)
        assert result.knowledge_compilation == "stopped", result
        assert time.monotonic() - stopped_at[0] < 2
        assert len(calls) == concurrency
        assert result.usage["observable_attempts"] >= concurrency
        assert result.usage["unknown_usage"] == concurrency
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    finally:
        release.set()
        canceller.join(5)


@pytest.mark.parametrize("concurrency", [2, 8])
def test_parallel_failure_stops_admission_and_joins_siblings(concurrency):
    from openkb.agent.evidence_parallel import parallel_batches
    from openkb.processing import processing_checkpoint

    arrived = threading.Barrier(concurrency)
    started = []

    def operation(value):
        started.append(value)
        arrived.wait(timeout=2)
        if value == 0:
            raise ValueError("invalid evidence fixture")
        while True:
            processing_checkpoint()
            time.sleep(0.005)

    with pytest.raises(ValueError, match="invalid evidence fixture"):
        list(parallel_batches(range(20), operation, concurrency))
    assert sorted(started) == list(range(concurrency))
