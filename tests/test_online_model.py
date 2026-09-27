"""Configuration contracts for the explicit real-provider test entry point."""

import json
import os

import pytest

from tests.online_model import load_online_model


def _profile(tmp_path, name, key):
    kb = tmp_path / name
    (kb / ".openkb").mkdir(parents=True)
    (kb / ".openkb/config.yaml").write_text(
        f"model: openai/{name}\nplanning_thinking: disabled\n", encoding="utf-8"
    )
    (kb / ".env").write_text(
        f"LLM_API_KEY={key}\nOPENAI_API_BASE=https://provider.invalid/v1\n", encoding="utf-8"
    )
    return kb


def test_explicit_profile_keeps_model_and_credentials_together(tmp_path, monkeypatch):
    first = _profile(tmp_path, "first", "private-first")
    second = _profile(tmp_path, "second", "private-second")
    monkeypatch.setenv("OPENKB_TEST_MODEL_KB", str(first))
    before = dict(os.environ)
    a, b = load_online_model(), load_online_model(second)
    assert (a.settings["model"], a.bundle.api_key) == ("openai/first", "private-first")
    assert (b.settings["model"], b.bundle.api_key) == ("openai/second", "private-second")
    assert dict(os.environ) == before
    visible = repr(a) + json.dumps(a.description())
    assert "private-first" not in visible and "provider.invalid" not in visible


def test_online_setup_requires_explicit_existing_kb(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENKB_TEST_MODEL_KB", raising=False)
    with pytest.raises(ValueError, match="OPENKB_TEST_MODEL_KB"):
        load_online_model()
    with pytest.raises(ValueError, match="config.yaml"):
        load_online_model(tmp_path)


def test_missing_credentials_fail_before_dispatch(tmp_path, monkeypatch):
    from openkb import config

    kb = _profile(tmp_path, "missing", "")
    global_dir = tmp_path / "global"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", global_dir)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_dir / "global.yaml")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="credentials are unavailable"):
        load_online_model(kb)


@pytest.mark.parametrize("markdown", [False, True])
def test_online_audit_records_real_receipt_without_global_profiler(
    kb_dir, tmp_path, model_service, markdown
):
    import sys

    from openkb.agent import compiler
    from openkb.agent.evidence_wire import WireMessages
    from openkb.config import load_config, resolve_credential_bundle
    from openkb.processing import processing_scope
    from tests.http_model_fixture import ModelReply
    from tests.online_step3 import Audit

    expected = "# Overview\n\nAvailable navigation." if markdown else '{"sections": []}'
    model_service.respond = lambda _: ModelReply(expected)
    settings = load_config(kb_dir / ".openkb/config.yaml")
    audit = Audit(tmp_path, "private-test-secret")
    before = sys.getprofile()
    messages = WireMessages(
        [{"role": "user", "content": '{"stage":"index_structure","evidence":{}}'}], {}
    )
    with audit.capture(), processing_scope(settings):
        assert sys.getprofile() is before
        result = compiler._llm_call(
            settings["model"],
            messages,
            "index_structure",
            bundle=resolve_credential_bundle(kb_dir),
            decode_response=not markdown,
        )
    assert sys.getprofile() is before
    assert result == expected
    request = json.loads((tmp_path / "request-01.json").read_text())
    response = json.loads((tmp_path / "response-01.json").read_text())
    assert request["messages"] == messages
    assert response["usage"]["prompt_tokens"] > 0
    assert response["provider_content"] == expected
    assert response["decoded_content"] == expected
    assert response["finish_reason"] == "stop"
    assert len(audit.requests) == 1
