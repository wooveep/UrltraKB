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
