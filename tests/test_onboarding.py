"""Native pending-action behavior and leased execution use the same connection proof."""

import json
import os
import subprocess
import sys

import pytest


def test_native_onboarding_cancellation_switch_and_exactly_once_resume(tmp_path):
    pytest.importorskip("PySide6")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_onboarding.py",
            "--headless",
            "--output",
            str(tmp_path / "gui"),
        ],
        env=dict(os.environ, PYTHONPATH=".", QT_QPA_PLATFORM="offscreen"),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads((tmp_path / "gui/result.json").read_text())
    assert evidence["headless"] and len(evidence["checks"]) == 6
    assert evidence["verification_mode"] == "retained-history"
    assert evidence["model_probe_requests"] == 0


def test_leased_execution_rechecks_actual_snapshot_without_imposing_cli_gate(tmp_path, monkeypatch):
    from error_transport import ErrorTransport
    from test_model_connections import retained_connection_proof

    from ultrakb import config
    from ultrakb.application import model_connections
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.settings import apply_global_config_patch
    from ultrakb.application.settings_data import GlobalConfigPatchRequest
    from ultrakb.config_state import capture_config
    from ultrakb.locks import kb_ingest_lock

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global/global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    with ErrorTransport(status=401) as service:
        apply_global_config_patch(
            GlobalConfigPatchRequest(
                config={
                    "model": "deepseek/deepseek-flash",
                    "model_capacity": template_capacity(CONNECTION_TEMPLATES[2]),
                },
                api_key="test-private-key",
                openai_api_base=service.url,
            )
        )
        kb = tmp_path / "kb"
        initialize_kb(kb, seed_environment=False)
        with kb_ingest_lock(kb / ".ultrakb"), ExecutionContext().begin(kb):
            pass  # Local/API contexts do not require a wizard verification record.
        with kb_ingest_lock(kb / ".ultrakb"), pytest.raises(model_connections.ConnectionRequired):
            with ExecutionContext(require_verified_connection=True).begin(kb):
                pass

        # Persistence/identity input only: no probe or model response is replaced.
        assert not model_connections.capture_connection(kb).missing
        retained_connection_proof(kb)
        assert model_connections.read_connection_state(kb).state == "passed"
        with kb_ingest_lock(kb / ".ultrakb"):
            snapshot = capture_config(kb)
        # The proof uses the captured global state location even in a fresh worker.
        monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "other-profile")
        model_connections.require_verified_connection(snapshot)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "global")
        apply_global_config_patch(GlobalConfigPatchRequest(api_key="changed-private-key"))
        with kb_ingest_lock(kb / ".ultrakb"), pytest.raises(model_connections.ConnectionRequired):
            with ExecutionContext(require_verified_connection=True).begin(kb):
                pass
        assert service.requests == []
