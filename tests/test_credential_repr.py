"""Diagnostics must not render the secrets captured for model execution."""

import json
from dataclasses import asdict

from ultrakb.config import LlmCredentialBundle
from ultrakb.config_state import ConfigSnapshot
from ultrakb.llm_execution import RoleBindings


def test_captured_credentials_are_hidden_in_debug_representations():
    bundle = LlmCredentialBundle(
        api_key="diagnostic-secret-key",
        base_url="https://api.deepseek.com",
        extra_headers={"Authorization": "diagnostic-secret-header"},
        timeout=30,
    )
    binding = RoleBindings("deepseek/deepseek-flash", bundle, model_table={})
    snapshot = ConfigSnapshot(kb_dir="/example", _payload=json.dumps(asdict(bundle)))

    for captured in (bundle, binding, snapshot):
        assert "diagnostic-secret" not in repr(captured)

    assert "timeout=30" in repr(bundle)
    assert binding.credentials() == bundle
    assert snapshot.values() == asdict(bundle)
