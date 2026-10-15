"""HTTP query and chat expose original figure metadata without a pixel tool."""

import pytest
from PIL import Image
from test_api import _auth, _client, _use_named_kb
from test_original_context import pages

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("endpoint", ["query", "chat"])
@pytest.mark.parametrize("max_calls", [1, 3])
def test_http_answer_retains_text_only_image_coverage(
    kb_dir, model_service, monkeypatch, endpoint, max_calls
):
    from agents import set_tracing_disabled

    from ultrakb.config import load_config, save_config

    set_tracing_disabled(True)
    path = kb_dir / ".ultrakb/config.yaml"
    config = load_config(path)
    config.update(
        model="deepseek/deepseek-flash", model_policy={"max_calls": max_calls, "retries": 0}
    )
    config["role_generation"] = {"answer": {"max_tokens": 256, "reasoning_effort": "none"}}
    save_config(path, config)
    Image.new("RGB", (32, 32), "white").save(kb_dir / "wiki/sources/figure.png")
    pages(
        kb_dir,
        ["# Figure\nThe recorded caption says calibrate at 17 degrees."],
        ["sources/figure.png"],
    )
    client = _client(monkeypatch)
    kb = _use_named_kb(monkeypatch, kb_dir)
    response = client.post(
        f"/api/v1/{endpoint}",
        headers=_auth(),
        json={
            "kb": kb,
            "stream": False,
            "question" if endpoint == "query" else "message": (
                "Read index.md, then use get_page_content exactly once for manual page 1, "
                "offset 0, then quote "
                "the recorded calibration caption. Report that image pixels were not interpreted. "
                "If reading tools are unavailable, say the original remains unread."
            ),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(model_service.requests) <= max_calls
    assert "DSML" not in payload["answer"]
    if max_calls == 1:
        assert not model_service.requests[0].get("tools")
        assert not payload.get("image_coverage")  # No image tool was allowed to run.
        assert payload["answer"] and "unread" in payload["answer"].lower()
        return
    assert payload["image_coverage"][0]["interpreted"] is False
    assert payload["image_coverage"][0]["status"] == "unread"
    assert "17" in payload["answer"] and "calibrat" in payload["answer"].lower()
    import json

    assert "data:image" not in json.dumps(model_service.requests)
    assert all(
        t["function"]["name"] != "get_image"
        for request in model_service.requests
        for t in request.get("tools", [])
    )
