"""Capacity rules remain true after a real partial save and SDK request."""

import pytest
from error_transport import ErrorTransport

from ultrakb import config
from ultrakb.application.connection_capacity import template_capacity
from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.model_connections import verify_model_connection
from ultrakb.application.settings import (
    apply_global_config_patch,
    apply_kb_config_patch,
    read_settings_view,
)
from ultrakb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
from ultrakb.config_state import capture_config
from ultrakb.locks import kb_ingest_lock
from ultrakb.model_tokenizer import question_measure

pytestmark = pytest.mark.integration


@pytest.fixture
def capacity_scope(tmp_path, monkeypatch, request):
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    with ErrorTransport(status=401) as service:
        capacity = {
            **template_capacity(CONNECTION_TEMPLATES[2]),
            "context_window_tokens": 81920,
            "output_reserve_tokens": 16384,
        }
        apply_global_config_patch(
            GlobalConfigPatchRequest(
                config={"model": "deepseek/deepseek-flash", "model_capacity": capacity},
                api_key="offline-capacity-key",
                openai_api_base=service.url,
            )
        )
        kb = tmp_path / "kb"
        initialize_kb(kb, seed_environment=False)
        if request.param == "kb":
            apply_kb_config_patch(
                kb,
                KbConfigPatchRequest(
                    kb=str(kb), config={"model_capacity": capacity}, api_key="offline-capacity-key"
                ),
            )
        yield kb, request.param, service


def save_patch(kb, scope, **fields):
    if scope == "kb":
        apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), **fields))
    else:
        apply_global_config_patch(GlobalConfigPatchRequest(**fields))


@pytest.mark.parametrize("capacity_scope", ["global", "kb"], indirect=True)
def test_partial_capacity_conflict_cannot_save_or_rotate_credentials(capacity_scope):
    kb, scope, service = capacity_scope
    paths = (
        (config.GLOBAL_CONFIG_PATH, config.GLOBAL_CONFIG_DIR / ".env")
        if scope == "global"
        else (kb / ".ultrakb/config.yaml", kb / ".env")
    )
    before = [path.read_bytes() for path in paths]
    with pytest.raises(ValueError, match="context"):
        save_patch(
            kb,
            scope,
            config={"model_capacity": {"max_input_tokens": 65537}},
            api_key="rejected-capacity-key",
            openai_api_base=service.url + "/rejected",
        )
    assert [path.read_bytes() for path in paths] == before
    assert read_settings_view(kb).values.model_capacity.max_input_tokens == 65536
    assert service.requests == []


@pytest.mark.parametrize("capacity_scope", ["global", "kb"], indirect=True)
def test_valid_partial_capacity_reaches_snapshot_tokenizer_and_real_sdk(capacity_scope):
    kb, scope, service = capacity_scope
    save_patch(
        kb,
        scope,
        config={"model_capacity": {"context_window_tokens": 98304, "output_reserve_tokens": 32768}},
    )
    view = read_settings_view(kb)
    assert view.sources["model_capacity"] == scope
    assert view.values.model_capacity.max_input_tokens == 65536
    with kb_ingest_lock(kb / ".ultrakb"):
        snapshot = capture_config(kb)
    captured = snapshot.values()["effective"]["model_capacity"]
    assert captured["context_window_tokens"] == 98304
    assert captured["output_reserve_tokens"] == 32768
    measure = question_measure(snapshot)
    assert measure.input_limit == 65536
    measure.require_current("Readiness check.")
    state = verify_model_connection(kb)
    assert state.state == "failed" and state.code == "authentication"
    assert len(service.requests) == 1
    assert service.requests[0]["json"]["model"] == "deepseek-flash"
