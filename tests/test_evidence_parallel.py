"""One document can extract independent batches concurrently without racing publication."""

import json
import threading
import time

import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize("concurrency", [1, 2, 4, 8])
def test_single_document_uses_bounded_parallel_batches_and_keeps_all_checkpoints(
    kb_dir, tmp_path, monkeypatch, concurrency
):
    path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["processing"].update(context_tokens=4096, output_tokens=1024, concurrency=concurrency)
    path.write_text(yaml.safe_dump(settings))
    source = tmp_path / "parallel.md"
    source.write_text(
        "\n\n".join(f"Condition {i}: pressure must remain below 37 kPa." for i in range(40))
    )
    guard = threading.Lock()
    overlap = threading.Event()
    active = peak = 0
    inputs = []

    def completion(**kwargs):
        nonlocal active, peak
        payload = json.loads(kwargs["messages"][-1]["content"])
        if payload["stage"] == "facts":
            with guard:
                active += 1
                peak = max(peak, active)
                inputs.extend(payload["units"])
                if active == concurrency:
                    overlap.set()
            assert overlap.wait(3), "The configured fact workers did not overlap"
            time.sleep(0.01)
            with guard:
                active -= 1
        else:
            assert active == 0
        return response(evidence_response(payload))

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert peak == concurrency
    assert len(inputs) == len({unit["id"] for unit in inputs}) == 40
    root = kb_dir / ".openkb/source-store/compilation"
    saved = {path.stem for path in root.glob("*.json")}
    latest = json.loads(next((root / "latest").glob("*.json")).read_text())
    assert set(latest["checkpoints"]) == saved


@pytest.mark.parametrize("concurrency", [2, 8])
def test_stop_cancels_all_inflight_batches_without_publishing(
    kb_dir, tmp_path, monkeypatch, concurrency
):
    from openkb.cancellation import cancellation_scope

    path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["processing"].update(context_tokens=4096, output_tokens=1024, concurrency=concurrency)
    path.write_text(yaml.safe_dump(settings))
    source = tmp_path / "cancel-parallel.md"
    source.write_text("\n\n".join(f"Condition {i}: mandatory requirement." for i in range(40)))
    all_started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    guard = threading.Lock()
    calls = []

    def completion(**kwargs):
        with guard:
            calls.append(kwargs)
            if len(calls) == concurrency:
                all_started.set()
        release.wait(10)
        return response({})

    def cancel():
        all_started.wait(3)
        stopped.set()

    monkeypatch.setattr(litellm, "completion", completion)
    canceller = threading.Thread(target=cancel)
    canceller.start()
    started = time.monotonic()
    try:
        with cancellation_scope(stopped.is_set):
            result = import_document(kb_dir, source)
        assert result.knowledge_compilation == "stopped", result
        assert time.monotonic() - started < 2
        assert len(calls) == result.usage["observable_attempts"] == concurrency
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
