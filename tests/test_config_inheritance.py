"""A03/A04: real creation and settings mutations retain dynamic inheritance."""

import pytest
import yaml

from ultrakb import config
from ultrakb.application.knowledge_bases import initialize_kb, open_kb
from ultrakb.application.settings import (
    apply_global_config_patch,
    apply_kb_config_patch,
    read_global_config,
    read_settings_view,
)
from ultrakb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest


@pytest.fixture
def global_settings(tmp_path, monkeypatch):
    root = tmp_path / "settings"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", root)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", root / "global.yaml")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    apply_global_config_patch(
        GlobalConfigPatchRequest(
            config={"model": "global-model", "language": "zh"},
            api_key="global-private-key",
            openai_api_base="https://global.example/v1",
        )
    )
    return root


def test_new_kb_tracks_global_changes_without_copying_credentials(tmp_path, global_settings):
    kb = tmp_path / "new"
    initialize_kb(kb, seed_environment=False)
    assert yaml.safe_load((kb / ".ultrakb/config.yaml").read_text()) == {}
    assert not (kb / ".env").exists()
    before = read_settings_view(kb)
    assert before.values.model == "global-model" and before.values.has_api_key
    assert before.sources["model"] == before.sources["api_key"] == "global"
    apply_global_config_patch(GlobalConfigPatchRequest(config={"model": "new-global"}))
    assert read_settings_view(kb).values.model == "new-global"


def test_explicit_overrides_survive_reopen_and_restore_inheritance(tmp_path, global_settings):
    kb = tmp_path / "explicit"
    initialize_kb(kb, model="local-model", api_key="local-private-key", seed_environment=False)
    before = (kb / ".ultrakb/config.yaml").read_bytes()
    apply_global_config_patch(GlobalConfigPatchRequest(config={"model": "new-global"}))
    open_kb(kb)
    assert (kb / ".ultrakb/config.yaml").read_bytes() == before
    view = read_settings_view(kb)
    assert view.values.model == "local-model" and view.values.language == "zh"
    assert view.sources["model"] == view.sources["api_key"] == "kb"
    apply_kb_config_patch(
        kb, KbConfigPatchRequest(kb=str(kb), config={"model": None}, api_key=None)
    )
    view = read_settings_view(kb)
    assert view.values.model == "new-global" and view.sources["api_key"] == "global"
    assert read_global_config().model == "new-global" and read_global_config().has_api_key


def test_environment_origin_matches_effective_credentials_and_global_save(
    tmp_path, global_settings, monkeypatch
):
    monkeypatch.setenv("LLM_API_KEY", "environment-private-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://environment.example/v1")
    kb = tmp_path / "inherited"
    initialize_kb(kb, seed_environment=False)
    apply_global_config_patch(GlobalConfigPatchRequest(api_key="new-global-private-key"))
    for root in (None, kb):
        view = read_settings_view(root)
        assert view.sources["api_key"] == view.sources["openai_api_base"] == "environment"
        assert view.values.has_api_key
        assert view.values.openai_api_base == "https://environment.example/v1"
        assert "private-key" not in view.model_dump_json()
    assert read_global_config().openai_api_base == "https://global.example/v1"
    apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), api_key="local-private-key"))
    bundle = config.resolve_credential_bundle(kb)
    assert bundle.api_key == "local-private-key"
    assert read_settings_view(kb).sources["api_key"] == "kb"
    assert "private-key" not in repr(bundle)
    apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), api_key=None))
    assert read_settings_view(kb).sources["api_key"] == "environment"
    monkeypatch.delenv("LLM_API_KEY")
    assert read_settings_view(kb).sources["api_key"] == "global"


def test_language_is_explicit_without_pinning_model(tmp_path, global_settings):
    kb = tmp_path / "language"
    initialize_kb(kb, language="fr", seed_environment=False)
    assert read_settings_view(kb).values.model == "global-model"
    assert read_settings_view(kb).values.language == "fr"
