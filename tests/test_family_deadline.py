"""Task-family remaining model time bounds the actual SDK transport."""

import asyncio
import time
from dataclasses import replace
from uuid import uuid4

import pytest

from openkb.config import load_config, save_config
from openkb.processing import ProcessingIncomplete, RequestLimits
from openkb.runtime.family_budget import current_family, family_scope


@pytest.mark.asyncio
async def test_transport_timeout_does_not_masquerade_as_expired_allowance():
    from openkb.agent.request_budget import model_deadline

    with pytest.raises(TimeoutError, match="transport"):
        async with model_deadline():
            raise TimeoutError("transport")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["request", "token", "time"])
async def test_exhausted_family_stops_before_stream_dispatch(
    kb_dir, tmp_path, model_service, reason
):
    from openkb.application.conversations import ask_question

    settings = load_config(kb_dir / ".openkb/config.yaml")
    limits = RequestLimits.from_config(settings)
    limits = replace(
        limits,
        **{
            "request": {"max_requests": 1},
            "token": {"max_tokens": 1},
            "time": {"document_timeout": 1},
        }[reason],
    )
    with family_scope(tmp_path / "history/receipts", uuid4().hex):
        current_family().reserve(limits, 1, "earlier", 1)
        result = await asyncio.wait_for(ask_question(kb_dir, "Question", save=True), 3)
    assert result.status != "completed"
    assert result.saved_path is None
    assert reason + "_budget_exhausted" in result.error
    assert len(model_service) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_request_uses_remaining_family_deadline(kb_dir, tmp_path, model_service, stream):
    from openkb.agent.query import build_run_config_from_bundle, run_query
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    path = kb_dir / ".openkb/config.yaml"
    settings = load_config(path)
    settings["processing"].update(document_timeout=10, request_timeout=5)
    save_config(path, settings)
    model_service.chat_response = lambda body: {"role": "assistant", "content": "Long answer."}
    model_service.chat_without_tools = True
    model_service.release.clear()
    with (
        family_scope(tmp_path / "history/receipts", uuid4().hex),
        kb_ingest_lock(kb_dir / ".openkb"),
        ExecutionContext().begin(kb_dir) as bundle,
    ):
        current_family().reserve(RequestLimits.from_config(settings), 0, "earlier", 9.8)
        started = time.monotonic()
        try:
            with pytest.raises(ProcessingIncomplete, match="time_budget_exhausted"):
                await asyncio.wait_for(
                    run_query(
                        "Question",
                        kb_dir,
                        settings["model"],
                        stream=stream,
                        bundle=bundle,
                        run_config=build_run_config_from_bundle(settings["model"], bundle),
                    ),
                    3,
                )
        finally:
            model_service.release.set()
        assert time.monotonic() - started < 3
        # Cold provider initialization may exhaust the allowance before dispatch.
        assert len(model_service) <= 1


@pytest.mark.asyncio
async def test_dripping_answer_cannot_outlive_family_allowance(kb_dir, tmp_path, model_service):
    from openkb.application.conversations import ask_question

    path = kb_dir / ".openkb/config.yaml"
    settings = load_config(path)
    settings["processing"].update(document_timeout=10, request_timeout=5)
    save_config(path, settings)
    model_service.chat_response = lambda body: {"role": "assistant", "content": "answer " * 20}
    model_service.chat_without_tools = True
    model_service.drip_seconds = 0.05
    with family_scope(tmp_path / "history/receipts", uuid4().hex):
        current_family().reserve(RequestLimits.from_config(settings), 0, "earlier", 9.8)
        result = await asyncio.wait_for(ask_question(kb_dir, "Question", save=True), 3)
    assert result.status != "completed"
    assert result.saved_path is None
    assert "time_budget_exhausted" in result.error
    assert len(model_service) == 1
    # Cleanup removes the expired run's holder before the next independent run.
    model_service.drip_seconds = 0
    next_result = await ask_question(kb_dir, "Another question", save=True)
    assert next_result.status == "completed"
