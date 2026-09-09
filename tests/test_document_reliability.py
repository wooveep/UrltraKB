"""Document outcomes through the shared use case, with only the model replaced."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
import yaml

from openkb.application.documents import import_document


@pytest.fixture
def processing_config(kb_dir):
    config = {
        "model": "openai/offline-test",
        "processing": {
            "request_timeout": 2,
            "stage_timeout": 5,
            "document_timeout": 10,
            "cleanup_timeout": 1,
            "max_attempts": 2,
            "max_requests": 20,
            "max_tokens": 100000,
            "concurrency": 2,
            "context_tokens": 32768,
            "output_tokens": 1024,
        },
    }
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(config))
    return config


def test_unfinished_compilation_preserves_previously_committed_knowledge(
    kb_dir, monkeypatch, processing_config
):
    import litellm

    responses = iter(
        [
            json.dumps({"description": "Notes", "content": "# Notes\nCandidate summary."}),
            "not a usable plan",
        ]
    )

    def completion(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=next(responses)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(litellm, "completion", completion)
    source = kb_dir / "notes.md"
    source.write_text("# Notes\nOriginal knowledge.")
    previous = kb_dir / "wiki/concepts/previous.md"
    previous.write_text("Previously committed knowledge")
    result = import_document(kb_dir, source)

    assert result.status == "unfinished"
    assert result.knowledge_compilation == "unfinished"
    assert result.reason == "concept_plan_unparseable"
    assert previous.read_text() == "Previously committed knowledge"
    assert not (kb_dir / "wiki/summaries/notes.md").exists()


def test_slow_summary_keeps_auxiliary_callbacks_responsive(kb_dir, monkeypatch, processing_config):
    import litellm

    callbacks = []
    responses = iter(
        [
            json.dumps({"description": "Notes", "content": "# Notes\nKnowledge."}),
            json.dumps({"create": [], "update": [], "related": []}),
        ]
    )

    def completion(**kwargs):
        time.sleep(0.15)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=next(responses)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    def observe(event):
        # An independent callback on the compiler's real event loop. The
        # network adapter schedules it, as SDK logging callbacks would.
        callbacks.append(time.monotonic())

    original = asyncio.BaseEventLoop.run_until_complete

    def run_until_complete(loop, future):
        if not callbacks:
            loop.call_later(0.05, observe, None)
        return original(loop, future)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(asyncio.BaseEventLoop, "run_until_complete", run_until_complete)
    source = kb_dir / "notes.md"
    source.write_text("Original knowledge.")
    started = time.monotonic()
    result = import_document(kb_dir, source)
    assert result.status == "added"
    assert callbacks and callbacks[0] - started < 0.2


@pytest.mark.parametrize("suffix", ["md", "pdf"])
def test_full_request_over_budget_is_unfinished_without_a_model_attempt(
    kb_dir, monkeypatch, processing_config, suffix
):
    import litellm

    def unexpected(**kwargs):
        pytest.fail("An over-budget request reached the model")

    monkeypatch.setattr(litellm, "completion", unexpected)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test")
    processing_config["processing"]["context_tokens"] = 1025
    processing_config["pageindex_threshold"] = 1
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(processing_config))
    source = kb_dir / f"notes.{suffix}"
    if suffix == "pdf":
        import fitz

        with fitz.open() as pdf:
            for number in range(2):
                pdf.new_page().insert_text((72, 72), f"Chapter {number + 1}. Original knowledge.")
            pdf.save(source)
    else:
        source.write_text("Tiny document; the schema and output reserve still count.")
    result = import_document(kb_dir, source)
    assert result.status == "unfinished"
    assert result.reason == "input_budget_exceeded"
    assert not (kb_dir / "wiki/summaries/notes.md").exists()


def test_attempt_budget_prevents_whole_document_retry(kb_dir, monkeypatch, processing_config):
    import litellm

    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise litellm.ServiceUnavailableError(
            "offline", model="offline-test", llm_provider="openai"
        )

    monkeypatch.setattr(litellm, "completion", unavailable)
    processing_config["processing"]["max_requests"] = 1
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(processing_config))
    source = kb_dir / "notes.md"
    source.write_text("Original knowledge.")
    result = import_document(kb_dir, source)
    assert result.status == "unfinished"
    assert result.reason == "request_budget_exhausted"
    assert len(calls) == 1
    assert calls[0]["num_retries"] == calls[0]["max_retries"] == 0
    assert 0 < calls[0]["timeout"] <= 2
    assert calls[0]["max_tokens"] == 1024


def test_pdf_toc_fallback_keeps_same_loop_callbacks_live(kb_dir, monkeypatch, processing_config):
    import fitz
    import litellm

    from openkb.processing import ProcessingIncomplete

    processing_config["pageindex_threshold"] = 1
    processing_config["processing"]["max_requests"] = 30
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(processing_config))
    (kb_dir / ".env").write_text("LLM_API_KEY=synthetic-test\n")
    source = kb_dir / "manual.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Contents. Chapter A .... 2")
        pdf.new_page().insert_text((72, 72), "Chapter A. Original knowledge.")
        pdf.save(source)
    detections = 0
    transforms = 0
    heartbeat = []

    def response(value):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(value)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=20),
        )

    def completion(**kwargs):
        nonlocal detections, transforms
        prompt = str(kwargs["messages"])
        if "toc_detected" in prompt:
            detections += 1
            return response({"toc_detected": "yes" if detections == 1 else "no"})
        if "page_index_given_in_toc" in prompt:
            return response({"page_index_given_in_toc": "yes"})
        if "transform the whole table" in prompt:
            transforms += 1
            if transforms == 2:
                # First async verification rejected the numbered TOC. This is
                # the real synchronous fallback, reached inside PageIndex.
                time.sleep(0.15)
                assert heartbeat, "TOC fallback blocked its event loop"
                raise ProcessingIncomplete("stopped_after_verified_fallback", "indexing")
            return response(
                {"table_of_contents": [{"structure": "1", "title": "Chapter A", "page": 2}]}
            )
        if '"completed"' in prompt:
            return response({"completed": "yes"})
        return response(
            [{"structure": "1", "title": "Chapter A", "physical_index": "<physical_index_2>"}]
        )

    async def asynchronous(**kwargs):
        asyncio.get_running_loop().call_later(0.03, heartbeat.append, "responsive")
        return response({"answer": "no"})

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", asynchronous)
    result = import_document(kb_dir, source)
    assert result.status == "unfinished"
    assert result.reason == "stopped_after_verified_fallback"
    assert not (kb_dir / "wiki/summaries/manual.md").exists()
    assert result.usage["observable_attempts"] >= 6


def test_client_cleanup_warning_does_not_change_committed_result(
    kb_dir, monkeypatch, processing_config
):
    import litellm

    values = iter(
        [
            {"description": "Notes", "content": "# Notes"},
            {"create": [], "update": [], "related": []},
        ]
    )
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(next(values))), finish_reason="stop"
                )
            ],
            usage=None,
        ),
    )

    async def failed_close():
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(litellm, "close_litellm_async_clients", failed_close)
    source = kb_dir / "notes.md"
    source.write_text("Notes")
    result = import_document(kb_dir, source)
    assert result.status == "added"
    assert result.knowledge_compilation == "completed"
    assert result.warnings == ("model_client_cleanup_failed",)
    assert result.usage["unknown_usage"] == 2
    assert all(row["transport_attempts"] is None for row in result.usage["requests"])
