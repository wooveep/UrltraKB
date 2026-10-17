"""Real model HTTP errors reach application/API exits without committing content.

The actual LiteLLM and Agents SDK receive loopback 401/503 replies. No successful
model reply is generated here; saved prior turns are ordinary domain input.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import litellm
import pytest
from error_transport import ErrorTransport
from fastapi.testclient import TestClient

from ultrakb.agent.chat_session import ChatSession, list_sessions, load_session
from ultrakb.application.conversations import ask_question, continue_conversation, read_conversation
from ultrakb.config import load_config, save_config
from ultrakb.llm_usage import aggregate_usage, read_requests
from ultrakb.locks import atomic_write_text

pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_application(kb_dir, tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    monkeypatch.setattr("ultrakb.config.GLOBAL_CONFIG_DIR", profile)
    monkeypatch.setattr("ultrakb.config.GLOBAL_CONFIG_PATH", profile / "global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "local-error-test-token")
    monkeypatch.setattr("ultrakb.api.load_dotenv", lambda: False)
    monkeypatch.setattr("ultrakb.api_helpers.resolve_kb_alias", lambda _: kb_dir)
    real_env = Path(__file__).resolve().parents[1] / ".env"
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.resolve() != real_env, "Offline error tests must never read project credentials"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    original_completion = litellm.acompletion
    original_sync_completion = litellm.completion
    sdk_errors = []

    async def observed_completion(*args, **kwargs):
        try:
            return await original_completion(*args, **kwargs)
        except Exception as error:
            sdk_errors.append(error)
            raise

    def observed_sync_completion(*args, **kwargs):
        try:
            return original_sync_completion(*args, **kwargs)
        except Exception as error:
            sdk_errors.append(error)
            raise

    monkeypatch.setattr(litellm, "acompletion", observed_completion)
    monkeypatch.setattr(litellm, "completion", observed_sync_completion)
    atomic_write_text(kb_dir / "wiki/sources/original.md", "The retained original states 42.")
    atomic_write_text(
        kb_dir / "wiki/concepts/manual.md", "# Manual note\n\nKeep this human note.\n"
    )
    atomic_write_text(kb_dir / "wiki/log.md", "# Operations Log\n\nKeep this prior audit note.\n")
    return sdk_errors


def _configure_model(kb_dir, service):
    path = kb_dir / ".ultrakb/config.yaml"
    config = load_config(path)
    config.update(model="deepseek/deepseek-flash", language="en", timeout=5)
    config["model_policy"] = {"max_calls": 8, "retries": 2, "backoff": 0}
    config["role_generation"] = {"answer": {"max_tokens": 32, "reasoning_effort": "none"}}
    save_config(path, config)
    atomic_write_text(
        kb_dir / ".env", f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n"
    )


def _wiki_bytes(kb_dir):
    return {
        path.relative_to(kb_dir / "wiki").as_posix(): path.read_bytes()
        for path in (kb_dir / "wiki").rglob("*")
        if path.is_file()
    }


def _assert_content_preserved(kb_dir, before):
    after = _wiki_bytes(kb_dir)
    assert {path: data for path, data in after.items() if path != "log.md"} == {
        path: data for path, data in before.items() if path != "log.md"
    }
    # The attempted chat is audited even when its model fails. Appending that
    # event is allowed; previous human/audit content must remain byte-identical.
    assert after["log.md"].startswith(before["log.md"])
    assert not list((kb_dir / "wiki/explorations").glob("*.md"))


def _assert_failed_sends(kb_dir, service, sdk_errors, *, status, sends):
    error_type = litellm.AuthenticationError if status == 401 else litellm.ServiceUnavailableError
    assert len(service.requests) == len(sdk_errors) == sends
    assert all(
        isinstance(error, error_type) and error.status_code == status for error in sdk_errors
    )
    assert all(service.body["error"]["message"] in str(error) for error in sdk_errors)
    assert all(item["path"].endswith("/chat/completions") for item in service.requests)
    assert all(item["json"]["model"] == "deepseek-flash" for item in service.requests)
    records = read_requests(kb_dir)
    assert len(records) == sends
    assert len({record.logical_call_id for record in records}) == 1
    assert all(record.send_status == "sent" and record.state == "failed" for record in records)
    assert all(record.business_state == "failed" for record in records)
    assert all(record.usage_status == "unknown" for record in records)
    assert all(record.input_total is None and record.output_total is None for record in records)
    summary = aggregate_usage(kb_dir)
    assert summary["requests"] == summary["actual_sends"] == summary["unknown_requests"] == sends
    assert summary["in_flight_requests"] == 0 and not summary["usage_complete"]
    return error_type


def _existing_conversation(kb_dir, operation):
    if operation != "existing-chat":
        return None
    # Saved domain state, not a replacement SDK completion for this test.
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.record_turn(
        "Earlier saved question",
        "Earlier saved answer",
        [
            {"role": "user", "content": "Earlier saved question"},
            {"role": "assistant", "content": "Earlier saved answer"},
        ],
        usage={"input_cached": 3, "input_uncached": 5, "output": 2, "reasoning": 0},
    )
    return deepcopy(session)


def _assert_conversation_not_committed(kb_dir, operation, previous, question):
    sessions = list_sessions(kb_dir)
    if operation == "query":
        assert sessions == []
        return
    assert len(sessions) == 1
    saved = load_session(kb_dir, sessions[0]["id"])
    restored = read_conversation(kb_dir, saved.id)
    expected_turns = (("Earlier saved question", "Earlier saved answer"),) if previous else ()
    assert restored.turns == expected_turns
    assert restored.timeline == (*expected_turns, (question, "这次回答未完成，可以继续提问。"))
    if previous:
        assert saved.id == previous.id and saved.turn_count == previous.turn_count == 1
        assert saved.turns == previous.turns
        assert saved.history == previous.history
    else:
        assert saved.turn_count == 0 and saved.history == []


@pytest.mark.parametrize(("status", "sends"), [(401, 1), (503, 3)])
@pytest.mark.parametrize("operation", ["query", "new-chat", "existing-chat"])
def test_application_reports_received_model_errors_without_retry_leaks_or_content_writes(
    kb_dir, isolated_application, status, sends, operation, request
):
    question = "Read the original measurement."
    previous = _existing_conversation(kb_dir, operation)
    before = _wiki_bytes(kb_dir)
    with ErrorTransport(status=status) as service:
        _configure_model(kb_dir, service)
        result = asyncio.run(
            ask_question(kb_dir, question, save=True)
            if operation == "query"
            else continue_conversation(
                kb_dir, question, session_id=previous.id if previous else None
            )
        )
        error_type = _assert_failed_sends(
            kb_dir, service, isolated_application, status=status, sends=sends
        )
        request.node.user_properties.extend(
            [
                ("upstream_status", status),
                ("local_model_posts", len(service.requests)),
                ("application_error_exit", result.error),
                ("model_stages", ",".join(sorted({item.stage for item in read_requests(kb_dir)}))),
            ]
        )
    assert result.status == "failed" and result.answer == ""
    assert error_type.__name__ in result.error
    assert result.saved_path is None and result.changes == ()
    assert result.unfinished
    assert result.model_usage["current"]["actual_sends"] == sends
    assert result.model_usage["current"]["unknown_requests"] == sends
    assert result.usage == dict.fromkeys(("input_cached", "input_uncached", "output", "reasoning"))
    _assert_content_preserved(kb_dir, before)
    _assert_conversation_not_committed(kb_dir, operation, previous, question)


def _api_request(kb_dir, question, *, operation, previous, stream):
    from ultrakb.api import create_app

    endpoint = "query" if operation == "query" else "chat"
    payload = {"kb": "local", "stream": stream}
    if endpoint == "query":
        payload.update(question=question, save=True)
    else:
        payload["message"] = question
        if previous:
            payload["session_id"] = previous.id
    with TestClient(create_app()) as client:
        return client.post(
            f"/api/v1/{endpoint}",
            json=payload,
            headers={"Authorization": "Bearer local-error-test-token"},
        )


def _sse_events(response):
    events = []
    for block in response.text.strip().split("\n\n"):
        event_line, data_line = block.splitlines()
        assert event_line.startswith("event: ") and data_line.startswith("data: ")
        events.append((event_line.removeprefix("event: "), json.loads(data_line[6:])))
    return events


@pytest.mark.parametrize(("status", "sends"), [(401, 1), (503, 3)])
@pytest.mark.parametrize("operation", ["query", "new-chat", "existing-chat"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_api_error_exits_preserve_received_status_retry_bound_and_committed_state(
    kb_dir, isolated_application, status, sends, operation, stream, request
):
    question = "Read the original measurement."
    previous = _existing_conversation(kb_dir, operation)
    before = _wiki_bytes(kb_dir)
    with ErrorTransport(status=status) as service:
        _configure_model(kb_dir, service)
        response = _api_request(
            kb_dir, question, operation=operation, previous=previous, stream=stream
        )
        error_type = _assert_failed_sends(
            kb_dir, service, isolated_application, status=status, sends=sends
        )
        request.node.user_properties.extend(
            [
                ("upstream_status", status),
                ("local_model_posts", len(service.requests)),
                ("model_stages", ",".join(sorted({item.stage for item in read_requests(kb_dir)}))),
            ]
        )
    if stream:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(response)
        assert events[0][0] == "start"
        assert events[0][1]["endpoint"] == ("query" if operation == "query" else "chat")
        assert events[-1] == ("done", {})
        errors = [data for name, data in events if name == "error"]
        assert len(errors) == 1 and error_type.__name__ in errors[0]["message"]
        assert not any(name in {"delta", "final"} for name, data in events)
        usages = [data for name, data in events if name == "model_usage"]
        complete = [data for data in usages if data["current"].get("request_ids_complete", True)]
        assert len(complete) == 1 and complete[0] is usages[-1]
        assert any(not data["current"].get("request_ids_complete", True) for data in usages)
        assert not any(name == "model_usage_progress" for name, _ in events)
        assert set(complete[0]["current"]["request_ids"]) == {
            record.request_attempt_id for record in read_requests(kb_dir)
        }
        assert complete[0]["current"]["requests"] == len(complete[0]["current"]["request_ids"])
        assert complete[0]["cumulative"].get("request_ids_complete", True)
        assert set(complete[0]["cumulative"]["request_ids"]) == set(
            complete[0]["current"]["request_ids"]
        )
        assert usages[-1]["current"]["actual_sends"] == sends
        assert usages[-1]["current"]["unknown_requests"] == sends
        assert usages[-1]["current"]["in_flight_requests"] == 0
        if operation != "query":
            assert events[0][1]["session_id"] == list_sessions(kb_dir)[0]["id"]
    else:
        assert response.status_code == 500  # Model failure, not REST bearer rejection.
        body = response.json()
        assert error_type.__name__ in body["detail"]
        assert "answer" not in body and "saved_path" not in body
    _assert_content_preserved(kb_dir, before)
    _assert_conversation_not_committed(kb_dir, operation, previous, question)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["query", "new-chat"])
async def test_answer_stream_model_stop_keeps_one_complete_terminal_receipt(
    kb_dir, isolated_application, operation
):
    import threading

    from ultrakb.application.streams import answer_events

    gate = threading.Event()
    entered = asyncio.Event()
    contexts = []
    observed = []

    async def operation_with_context(context):
        contexts.append(context)
        return await (
            ask_question(kb_dir, "Read the original measurement.", context=context)
            if operation == "query"
            else continue_conversation(kb_dir, "Read the original measurement.", context=context)
        )

    async def consume():
        async for event in answer_events(operation_with_context):
            observed.append(event)
            if event.get("event") == "model_usage":
                entered.set()

    with ErrorTransport(status=400, gate=gate) as service:
        _configure_model(kb_dir, service)
        consumer = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(service.wait_for_requests, 1)
            await asyncio.wait_for(entered.wait(), timeout=3)
            contexts[0].executor.cancel()
            # Let the application's 50-ms stop polling observe cancellation
            # while HTTP is still gated, then deliver within its 250-ms drain.
            await asyncio.sleep(0.1)
            gate.set()
            await asyncio.wait_for(consumer, timeout=10)
        finally:
            gate.set()
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
    usages = [row["data"] for row in observed if row.get("event") == "model_usage"]
    complete = [row for row in usages if row["current"].get("request_ids_complete", True)]
    assert len(complete) == 1 and complete[0] is usages[-1]
    [result] = [row["data"] for row in observed if row.get("event") == "result"]
    assert result.status == "stopped" and result.model_usage == complete[0]
    [record] = read_requests(kb_dir)
    assert record.business_state == "cancelled"
    assert complete[0]["current"]["request_ids"] == [record.request_attempt_id]
    assert complete[0]["current"]["actual_sends"] == 1
    assert complete[0]["current"]["in_flight_requests"] == 0
