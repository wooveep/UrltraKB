"""A saved, tested connection remains usable when identical values are inherited/copied."""

import pytest

from ultrakb import config
from ultrakb.application import model_connections as connections
from ultrakb.application.connection_capacity import template_capacity
from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.settings import apply_global_config_patch, apply_kb_config_patch
from ultrakb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest


@pytest.mark.parametrize("override", ["model", "api_key", "openai_api_base"])
@pytest.mark.parametrize("legacy", [False, True])
def test_verified_connection_reused_by_identical_kb_settings(
    tmp_path, monkeypatch, override, legacy
):
    root = tmp_path / "settings"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", root)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", root / "global.yaml")
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    values = {
        "model": "openai/example",
        "api_key": "fixture-private-key",
        "openai_api_base": "https://example.test/v1",
    }
    apply_global_config_patch(
        GlobalConfigPatchRequest(
            config={
                "model": values["model"],
                "model_capacity": {
                    **template_capacity(CONNECTION_TEMPLATES[2]),
                    "applies_to_model": values["model"],
                },
            },
            api_key=values["api_key"],
            openai_api_base=values["openai_api_base"],
        )
    )
    probes = []

    async def probe(connection, *_):
        probes.append(connection.model)

    monkeypatch.setattr(connections, "_probe", probe)
    assert connections.verify_model_connection().state == "passed"
    if legacy:
        connection = connections.capture_connection()
        (root / "connections" / f"{connection.identity}.json").rename(
            root / "connections" / f"{connection.legacy_identity}.json"
        )
    kb = tmp_path / "kb"
    initialize_kb(kb, seed_environment=False)
    patch = (
        {"config": {"model": values[override]}}
        if override == "model"
        else {override: values[override]}
    )
    apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), **patch))
    assert connections.read_connection_state(kb).state == "passed", (
        "Sending from this KB must reuse the tested connection instead of reopening setup"
    )
    assert probes == [values["model"]]
