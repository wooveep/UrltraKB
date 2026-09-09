"""Document outcomes through the shared use case, with only the model replaced."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
import yaml

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response


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

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        content = (
            json.dumps(evidence_response(payload))
            if payload["stage"] == "facts"
            else "not a usable plan"
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")
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
    assert result.reason == "topic_plan_invalid"
    assert previous.read_text() == "Previously committed knowledge"
    assert not (kb_dir / "wiki/summaries/notes.md").exists()


def test_slow_compilation_keeps_async_recompile_callbacks_responsive(
    kb_dir, monkeypatch, processing_config
):
    import litellm

    from openkb.application.recompilation import recompile_document

    callbacks = []

    def completion(**kwargs):
        time.sleep(0.15)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            evidence_response(json.loads(kwargs["messages"][-1]["content"]))
                        )
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(litellm, "completion", completion)
    (kb_dir / "wiki/sources/notes.md").write_text("Original knowledge.")
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps({"h": {"doc_name": "notes", "type": "md"}})
    )

    async def compile_with_callback():
        asyncio.get_running_loop().call_later(0.05, lambda: callbacks.append(time.monotonic()))
        return await recompile_document(kb_dir, "h")

    started = time.monotonic()
    result = asyncio.run(compile_with_callback())
    assert result.status == "unfinished" and result.message == "needs_acceptance"
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
    assert result.reason == "evidence_context_exceeds_request_budget"
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


def test_pdf_local_navigation_keeps_its_model_loop_callbacks_live(
    kb_dir, monkeypatch, processing_config
):
    import fitz
    import litellm

    from openkb.application.source_history import source_status

    processing_config["navigation"] = {
        "enabled": True,
        "processing": processing_config["processing"],
    }
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(processing_config))
    source = kb_dir / "manual.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Contents. Chapter A .... 999")
        pdf.new_page().insert_text((72, 72), "Chapter A. Original knowledge.")
        pdf.save(source)
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
        return response(evidence_response(json.loads(kwargs["messages"][-1]["content"])))

    async def asynchronous(**kwargs):
        asyncio.get_running_loop().call_later(0.03, heartbeat.append, "responsive")
        await asyncio.sleep(0.15)
        assert heartbeat, "Navigation blocked its model event loop"
        return response("Local navigation summary")

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", asynchronous)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    navigation = source_status(kb_dir, result.source_id)["navigation"]
    assert navigation["status"] == "enhanced", navigation
    assert {position["location"]["page"] for position in navigation["positions"]} == {1, 2}
    assert navigation["usage"]["observable_attempts"] > 0
    assert heartbeat


def test_client_cleanup_warning_does_not_change_committed_result(
    kb_dir, monkeypatch, processing_config
):
    import litellm

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            evidence_response(json.loads(kwargs["messages"][-1]["content"]))
                        )
                    ),
                    finish_reason="stop",
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
    assert result.usage["unknown_usage"] == 3
    assert all(row["transport_attempts"] is None for row in result.usage["requests"])
