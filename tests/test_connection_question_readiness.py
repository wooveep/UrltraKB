"""Historical network proofs cannot replace the actual question prerequisites."""

import pytest
from error_transport import ErrorTransport

from ultrakb import config
from ultrakb.application import model_connections as connections
from ultrakb.application.connection_capacity import template_capacity
from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.settings import apply_global_config_patch, apply_kb_config_patch
from ultrakb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
from ultrakb.config_state import capture_config
from ultrakb.locks import atomic_write_json, kb_ingest_lock

pytestmark = pytest.mark.integration


@pytest.fixture
def old_verified_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    with ErrorTransport(status=401) as service:
        apply_global_config_patch(
            GlobalConfigPatchRequest(
                config={"model": "deepseek/deepseek-flash"},
                api_key="offline-readiness-key",
                openai_api_base=service.url,
            )
        )
        kb = tmp_path / "kb"
        initialize_kb(kb, seed_environment=False)
        connection = connections.capture_connection(kb)
        proof_path = connections._record_path(connection)
        # This is persisted domain input from an earlier verification. No current
        # model call or successful model reply is simulated by the record.
        atomic_write_json(
            proof_path,
            {
                "version": 1,
                "attempt": "a" * 32,
                "state": "passed",
                "tested_at": "2026-10-14T00:00:00+00:00",
                "capabilities": list(connections.CAPABILITIES),
            },
        )
        proof = proof_path.read_bytes()
        connections._VerificationRecord.model_validate_json(proof)
        yield kb, connection, proof_path, proof, service


@pytest.mark.parametrize("scope", ["global", "kb"])
def test_missing_question_capacity_blocks_an_old_pass_and_preserves_its_proof(
    old_verified_connection, scope
):
    kb, connection, proof_path, proof, service = old_verified_connection
    state = connections.read_connection_state(kb)
    assert state.state == "missing" and "容量" in state.message
    with kb_ingest_lock(kb / ".ultrakb"):
        snapshot = capture_config(kb)
        with pytest.raises(connections.ConnectionRequired):
            connections.require_verified_connection(snapshot)
        with pytest.raises(connections.ConnectionRequired):
            with ExecutionContext(require_verified_connection=True).begin(kb):
                pytest.fail("An old network proof admitted a question without capacity")

    capacity = template_capacity(CONNECTION_TEMPLATES[2])
    if scope == "global":
        apply_global_config_patch(GlobalConfigPatchRequest(config={"model_capacity": capacity}))
    else:
        apply_kb_config_patch(
            kb, KbConfigPatchRequest(kb=str(kb), config={"model_capacity": capacity})
        )
    assert connections.read_connection_state(kb).state == "passed"
    assert connections.capture_connection(kb).identity == connection.identity
    assert proof_path.read_bytes() == proof
    with kb_ingest_lock(kb / ".ultrakb"):
        connections.require_verified_connection(capture_config(kb))
    assert service.requests == []


@pytest.mark.parametrize("case", ["input_only", "tokenizer_only", "wrong_binding", "custom_model"])
def test_unusable_capacity_cannot_probe_or_reuse_a_pass(old_verified_connection, case):
    kb, _connection, proof_path, proof, service = old_verified_connection
    capacity = template_capacity(CONNECTION_TEMPLATES[2])
    patch = {"model_capacity": capacity}
    if case == "input_only":
        capacity.pop("tokenizer_model")
    elif case == "tokenizer_only":
        capacity.pop("max_input_tokens")
    elif case == "wrong_binding":
        capacity["applies_to_model"] = "private/another-model"
    else:
        patch = {"model": "openai/private-model", "model_capacity": None}
    apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), config=patch))
    state = connections.verify_model_connection(kb)
    assert state.state == "missing" and "容量" in state.message
    assert service.requests == []
    assert proof_path.read_bytes() == proof
