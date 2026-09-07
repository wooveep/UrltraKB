"""Settings behavior shared by local adapters, using real KB files."""

import pytest


def test_invalid_credential_patch_does_not_partially_save_model(kb_dir):
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest

    previous = read_kb_config(kb_dir)
    before = (kb_dir / ".openkb/config.yaml").read_text()
    patch = KbConfigPatchRequest(kb=str(kb_dir), config={"model": "new"}, api_key="bad\nkey")
    with pytest.raises(ValueError, match="newline"):
        apply_kb_config_patch(kb_dir, patch)
    assert read_kb_config(kb_dir) == previous
    assert (kb_dir / ".openkb/config.yaml").read_text() == before
    assert not (kb_dir / ".env").exists()


def test_settings_patch_preserves_three_states_and_hides_secret(kb_dir, tmp_path, monkeypatch):
    from openkb import config
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    config.save_global_config({"model": "inherited"})
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb="kb", api_key="private-test-key"))
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb="kb", config={"model": "override"}))
    current = read_kb_config(kb_dir)
    assert current.model == "override"
    assert current.sources["model"] == "kb"
    assert current.has_api_key
    assert "private-test-key" not in current.model_dump_json()
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb="kb", config={"model": None}))
    current = read_kb_config(kb_dir)
    assert current.model == "inherited"
    assert current.sources["model"] == "global"
    assert current.has_api_key
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb="kb", api_key=None))
    assert "LLM_API_KEY" not in (kb_dir / ".env").read_text()
