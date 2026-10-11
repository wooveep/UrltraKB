"""HTTP query and chat expose original figure metadata without a pixel tool."""

import pytest
from PIL import Image
from test_api import _auth, _client, _use_named_kb
from test_original_context import pages
from test_vendor_sdk import streamed_response

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("endpoint", ["query", "chat"])
@pytest.mark.parametrize("max_calls", [1, 2])
def test_http_answer_retains_text_only_image_coverage(
    kb_dir, model_service, monkeypatch, endpoint, max_calls
):
    from agents import set_tracing_disabled

    from openkb.config import load_config, save_config

    set_tracing_disabled(True)
    monkeypatch.setenv("LLM_API_KEY", "fixture")
    path = kb_dir / ".openkb/config.yaml"
    config = load_config(path)
    config.update(model="deepseek/deepseek-flash", model_policy={"max_calls": max_calls})
    save_config(path, config)
    Image.new("RGB", (32, 32), "white").save(kb_dir / "wiki/sources/figure.png")
    pages(kb_dir, ["# Figure\nImage"], ["sources/figure.png"])
    call = {
        "id": "figure",
        "type": "function",
        "function": {
            "name": "get_page_content",
            "arguments": '{"doc_name":"manual","pages":"1","offset":0}',
        },
    }
    model_service.replies.append(
        (
            200,
            streamed_response(
                {"role": "assistant", "content": None, "tool_calls": [call]}, reason="tool_calls"
            ),
        )
    )
    if max_calls == 2:
        model_service.replies.append(
            (200, streamed_response({"role": "assistant", "content": "No pixels received."}))
        )
    client = _client(monkeypatch)
    kb = _use_named_kb(monkeypatch, kb_dir)
    response = client.post(
        f"/api/v1/{endpoint}",
        headers=_auth(),
        json={
            "kb": kb,
            "stream": False,
            "question" if endpoint == "query" else "message": "Read the figure",
        },
    )
    assert response.status_code == (200 if max_calls == 2 else 500)
    payload = response.json()
    if max_calls == 1:
        assert not model_service.requests[0].get("tools")
        assert not payload.get("image_coverage")  # No image tool was allowed to run.
        assert isinstance(payload["detail"], str)
        return
    assert payload["image_coverage"][0]["interpreted"] is False
    assert payload["image_coverage"][0]["status"] == "unread"
    import json

    assert "data:image" not in json.dumps(model_service.requests)
    assert all(
        t["function"]["name"] != "get_image"
        for request in model_service.requests
        for t in request.get("tools", [])
    )
