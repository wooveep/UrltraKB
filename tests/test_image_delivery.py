"""Retained figure tools/history cannot deliver pixels to main models."""

import json

import pytest
from agents import RunConfig, Runner
from test_llm_execution import executor_for
from test_managed_agent import agent_for

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_image_tool_output_is_rejected_before_provider_serialization(model_service, stream):
    executor = executor_for(model_service)
    history = [
        {"role": "user", "content": "Read a figure."},
        {"type": "function_call", "name": "get_image", "call_id": "figure", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "figure",
            "output": [
                {"type": "input_text", "text": "Original figure reference."},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        },
    ]
    with pytest.raises(ValueError, match="text-only"):
        if stream:
            run = Runner.run_streamed(
                agent_for(executor), history, run_config=RunConfig(tracing_disabled=True)
            )
            async for _ in run.stream_events():
                pass
        else:
            await Runner.run(
                agent_for(executor), history, run_config=RunConfig(tracing_disabled=True)
            )
    assert not model_service.requests and executor.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_old_image_output_stays_text_during_repeated_delivery_and_streaming(
    model_service, stream
):
    from ultrakb.agent.chat_session import sanitize_history

    history = [
        {"role": "user", "content": "Read a figure."},
        {
            "type": "function_call",
            "name": "get_image",
            "call_id": "figure",
            "arguments": '{"image_path":"sources/figure.png"}',
        },
        {
            "type": "function_call_output",
            "call_id": "figure",
            "output": [
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        },
    ]
    executor = executor_for(model_service, retries=0, max_calls=2)
    for _ in range(2):
        if stream:
            run = Runner.run_streamed(
                agent_for(executor),
                sanitize_history(history),
                run_config=RunConfig(tracing_disabled=True),
            )
            async for _ in run.stream_events():
                pass
        else:
            run = await Runner.run(
                agent_for(executor),
                sanitize_history(history),
                run_config=RunConfig(tracing_disabled=True),
            )
        assert run.final_output
    assert len(model_service.requests) == executor.calls == 2
    for request in model_service.requests:
        text = json.dumps(request)
        assert "data:image" not in text and "input_image" not in text
        assert "sources/figure.png" in text and "not interpreted" in text
