"""Independent visual connections through application settings and question execution."""

import pytest

from openkb.application.settings import apply_kb_config_patch, read_settings_view
from openkb.application.settings_data import KbConfigPatchRequest


@pytest.fixture(autouse=True)
def isolated_image_capabilities(tmp_path, monkeypatch):
    from openkb import config

    root = tmp_path / "global"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", root)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", root / "global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", root / "global.lock")


def test_saving_visual_connection_keeps_feature_off_and_never_reads_key_back(kb_dir):
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "image_understanding": {
                    "provider": "openai-compatible",
                    "model": "local-vision",
                    "endpoint": "http://127.0.0.1:8123/v1",
                    "supports_images": True,
                }
            },
            image_api_key="visual-only-secret",
        ),
    )
    view = read_settings_view(kb_dir)
    assert view.values.image_understanding.enabled is False
    assert view.values.image_understanding.provider == "openai-compatible"
    assert view.values.has_image_api_key
    assert view.sources["image_understanding"] == "kb"
    assert "visual-only-secret" not in view.model_dump_json()


@pytest.mark.asyncio
async def test_visual_connection_test_sends_sample_and_isolates_key_from_main(kb_dir, monkeypatch):
    import httpx

    from openkb.application.image_understanding import test_image_connection

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            api_key="main-only-secret",
            image_api_key="visual-only-secret",
            config={
                "image_understanding": {
                    "provider": "openai-compatible",
                    "model": "vision-test",
                    "endpoint": "http://127.0.0.1:8123/v1",
                    "supports_images": True,
                }
            },
        ),
    )
    requests = []

    async def send(client, request, **kwargs):
        import json

        requests.append((request.url, request.headers, json.loads(request.content)))
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "A red square."}, "finish_reason": "stop"}],
                "usage": None,
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    result = await test_image_connection(kb_dir)
    assert result["status"] == "ready"
    url, headers, body = requests[0]
    assert str(url) == "http://127.0.0.1:8123/v1/chat/completions"
    assert headers["authorization"] == "Bearer visual-only-secret"
    assert body["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;")
    assert read_settings_view(kb_dir).values.image_understanding.enabled is False

    async def rejected(client, request, **kwargs):
        return httpx.Response(400, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", rejected)
    assert (await test_image_connection(kb_dir))["status"] == "image_http_400"
    from openkb.vision.connection import capability_verified, resolve_connection

    assert not capability_verified(resolve_connection(kb_dir))
    # A changed destination cannot reuse the saved key of the previous connection.
    settings = read_settings_view(kb_dir).values.image_understanding.model_dump()
    settings["endpoint"] = "http://127.0.0.1:8124/v1"
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"image_understanding": settings})
    )
    assert not read_settings_view(kb_dir).values.has_image_api_key
    assert (await test_image_connection(kb_dir))["status"] == "image_credentials_missing"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("max_requests", [20, 1])
async def test_text_only_question_uses_visual_observation_without_receiving_image_payload(
    kb_dir, monkeypatch, max_requests
):
    import json

    import httpx
    from PIL import Image

    from openkb.application.conversations import ask_question
    from openkb.application.image_understanding import test_image_connection

    image_path = "sources/images/chart.png"
    Image.new("RGB", (40, 40), "red").save(kb_dir / "wiki" / image_path)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            api_key="text-key",
            openai_api_base="http://127.0.0.1:8111/v1",
            image_api_key="visual-key",
            config={
                "model": "openai/text-only-test",
                "image_understanding": {
                    "enabled": True,
                    "supports_images": True,
                    "provider": "openai-compatible",
                    "model": "vision-test",
                    "endpoint": "http://127.0.0.1:8123/v1",
                },
            },
        ),
    )
    processing = dict(read_settings_view(kb_dir).values.processing)
    processing["max_requests"] = max_requests
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"processing": processing})
    )
    sent = []

    async def send(client, request, **kwargs):
        body = json.loads(request.content)
        sent.append((str(request.url), dict(request.headers), body))
        if request.url.port == 8123:
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [
                        {"message": {"content": "The square is red."}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        observed = any(message["role"] == "tool" for message in body["messages"])
        delta = (
            {"content": "The square is red, according to the visual observation."}
            if observed
            else {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "look",
                        "type": "function",
                        "function": {
                            "name": "get_image",
                            "arguments": json.dumps(
                                {"image_path": image_path, "question": "What color is the square?"}
                            ),
                        },
                    }
                ]
            }
        )
        rows = [
            {
                "id": "answer",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "text-only-test",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "answer",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "text-only-test",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop" if observed else "tool_calls"}
                ],
            },
        ]
        stream = "".join("data: " + json.dumps(row) + "\n\n" for row in rows) + "data: [DONE]\n\n"
        return httpx.Response(
            200, request=request, content=stream, headers={"content-type": "text/event-stream"}
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    assert (await test_image_connection(kb_dir))["status"] == "ready"
    result = await ask_question(kb_dir, "What color is the square in sources/images/chart.png?")
    if max_requests == 1:
        assert result.status == "failed"
        assert len([row for row in sent if ":8123/" in row[0]]) == 1
        assert len([row for row in sent if ":8111/" in row[0]]) == 1
        return
    assert result.status == "completed", result
    visual = [row for row in sent if ":8123/" in row[0]]
    main = [row for row in sent if ":8111/" in row[0]]
    assert len(visual) == 2  # Built-in sample, then exactly the requested figure.
    assert all(row[1]["authorization"] == "Bearer visual-key" for row in visual)
    assert all(row[1]["authorization"] == "Bearer text-key" for row in main)
    assert "data:image" not in json.dumps(main)
    assert "visual_observation" in json.dumps(main)
    assert "visual-key" not in json.dumps(main)


@pytest.mark.asyncio
async def test_disabled_images_keep_old_conversation_readable_with_text_only_model(
    kb_dir, monkeypatch
):
    import json

    import httpx

    from openkb.agent.chat_session import ChatSession, load_session
    from openkb.application.conversations import continue_conversation

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            api_key="only-text-key",
            openai_api_base="http://127.0.0.1:8111/v1",
            config={"model": "openai/text-only-test"},
        ),
    )
    history = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Old question"},
                {"type": "input_image", "image_url": "data:image/png;base64,aGlzdG9yaWNhbA=="},
            ],
        },
        {"role": "assistant", "content": "Old answer"},
    ]
    session = ChatSession.new(kb_dir, "openai/old-vision", "en")
    session.record_turn("Old question", "Old answer", history)
    sent = []

    async def send(client, request, **kwargs):
        sent.append(json.loads(request.content))
        chunk = {
            "id": "text",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "text-only-test",
            "choices": [
                {"index": 0, "delta": {"content": "Text continuation."}, "finish_reason": "stop"}
            ],
        }
        return httpx.Response(
            200,
            request=request,
            content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    result = await continue_conversation(
        kb_dir, "Continue from the old answer.", session_id=session.id
    )
    assert result.status == "completed", result
    assert sent and "data:image" not in json.dumps(sent)
    assert all(tool["function"]["name"] != "get_image" for body in sent for tool in body["tools"])
    restored = load_session(kb_dir, session.id)
    assert any(image["base64"] == "aGlzdG9yaWNhbA==" for image in restored.history_images.values())
