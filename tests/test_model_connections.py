"""Connection state uses domain records; actual probes use the real SDK/model.

Offline endpoints can only fail. Persisted proof fixtures describe a prior
verification and never stand in for a successful SDK response in this run.
"""

import json
import logging
import threading
from dataclasses import asdict

import litellm
import pytest
from error_transport import ErrorTransport

from ultrakb import config
from ultrakb.application import model_connections as connections
from ultrakb.application.connection_capacity import template_capacity
from ultrakb.application.connection_failures import connection_failure
from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.settings import apply_global_config_patch
from ultrakb.application.settings_data import GlobalConfigPatchRequest
from ultrakb.locks import atomic_write_json


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / "global"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", root)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", root / "global.yaml")
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    capacity = template_capacity(CONNECTION_TEMPLATES[2])
    assert capacity["applies_to_model"] == "deepseek/deepseek-flash"
    apply_global_config_patch(
        GlobalConfigPatchRequest(
            config={
                "model": "deepseek/deepseek-flash",
                "model_capacity": capacity,
            },
            api_key="opaque-private-credential",
        )
    )
    assert not connections.capture_connection().missing
    return root


def retained_connection_proof(kb_dir=None, *, capabilities=connections.CAPABILITIES):
    """Seed domain state for identity/persistence checks, without claiming a probe ran."""
    connection = connections.capture_connection(kb_dir)
    atomic_write_json(
        connections._record_path(connection),
        {
            "version": 1,
            "attempt": "a" * 32,
            "state": "passed",
            "capabilities": list(capabilities),
            "tested_at": "2026-10-15T00:00:00+00:00",
        },
    )
    return connection


def use_error_service(service):
    apply_global_config_patch(GlobalConfigPatchRequest(openai_api_base=service.url))
    assert not connections.capture_connection().missing


@pytest.fixture
def observed_sdk(monkeypatch):
    """Observe completion/cleanup while preserving the real SDK result or error."""
    original = litellm.acompletion
    errors = []
    finished = threading.Event()

    async def complete(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except BaseException as error:
            errors.append(error)
            raise
        finally:
            finished.set()

    monkeypatch.setattr(litellm, "acompletion", complete)
    return errors, finished


@pytest.mark.integration
def test_readiness_never_probes_and_inheriting_kbs_reuse_pass(configured, tmp_path):
    with ErrorTransport(status=401) as service:
        use_error_service(service)
        assert connections.read_connection_state().state == "unverified"
        retained_connection_proof()
        assert connections.read_connection_state().state == "passed"
        for name in ("one", "two"):
            kb = tmp_path / name
            initialize_kb(kb, seed_environment=False)
            assert connections.read_connection_state(kb).state == "passed"
        assert service.requests == []
    records = [p.read_text() for p in (configured / "connections").glob("*.json")]
    assert len(records) == 1 and "opaque-private-credential" not in records[0]
    assert "opaque-private-credential" not in repr(connections.capture_connection())
    assert (configured / "connection-identity.key").stat().st_mode & 0o777 == 0o600


@pytest.mark.integration
@pytest.mark.parametrize("change", ["model", "key", "base", "request_parameters"])
def test_actual_connection_changes_invalidate_pass(configured, tmp_path, change):
    kb = tmp_path / "kb"
    initialize_kb(kb, seed_environment=False)
    retained_connection_proof(kb)
    assert connections.read_connection_state(kb).state == "passed"
    if change == "model":
        # Explicit capacity is test-domain input for this changed model name;
        # the shared UTF-8 bound never claims to discover an unknown server limit.
        capacity = template_capacity(CONNECTION_TEMPLATES[2])
        apply_global_config_patch(
            GlobalConfigPatchRequest(
                config={
                    "model": "openai/changed",
                    "model_capacity": {**capacity, "applies_to_model": "openai/changed"},
                }
            )
        )
    elif change == "key":
        apply_global_config_patch(GlobalConfigPatchRequest(api_key="rotated-private-credential"))
    elif change == "base":
        apply_global_config_patch(
            GlobalConfigPatchRequest(openai_api_base="https://other.example/v1")
        )
    else:
        with config._with_global_config_lock():
            config.save_config(kb / ".ultrakb/config.yaml", {"timeout": 5})
    assert not connections.capture_connection(kb).missing
    assert connections.read_connection_state(kb).state == "unverified"


@pytest.mark.integration
def test_late_error_cannot_mark_a_changed_connection_verified(configured, observed_sdk):
    release = threading.Event()
    results = []
    with ErrorTransport(status=401, gate=release) as service:
        use_error_service(service)
        worker = threading.Thread(
            target=lambda: results.append(connections.verify_model_connection(timeout=5)),
            daemon=True,
        )
        worker.start()
        try:
            assert service.wait_for_requests(1)
            apply_global_config_patch(
                GlobalConfigPatchRequest(api_key="rotated-private-credential")
            )
        finally:
            release.set()
            worker.join(6)
        assert not worker.is_alive()
        assert len(service.requests) == 1
    errors, finished = observed_sdk
    assert finished.is_set() and len(errors) == 1
    result = results[0]
    assert result.state == "unverified" and result.code == "changed"
    assert connections.read_connection_state().state == "unverified"
    assert "private-credential" not in json.dumps(asdict(result))


@pytest.mark.integration
@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_probe_stops_and_cleans_pending_model_work(configured, observed_sdk, action):
    stopped = threading.Event()
    release = threading.Event()
    results = []
    with ErrorTransport(status=401, gate=release) as service:
        use_error_service(service)
        worker = threading.Thread(
            target=lambda: results.append(
                connections.verify_model_connection(
                    cancelled=stopped.is_set, timeout=3 if action == "cancel" else 1
                )
            ),
            daemon=True,
        )
        worker.start()
        try:
            assert service.wait_for_requests(1)
            if action == "cancel":
                stopped.set()
            worker.join(4)
            assert not worker.is_alive()
            errors, finished = observed_sdk
            assert finished.is_set() and len(errors) == 1
            assert len(service.requests) == 1
        finally:
            release.set()
            worker.join(4)
    state = results[0]
    assert state.code == ("cancelled" if action == "cancel" else "timeout")
    assert state.state != "passed"
    assert connections.read_connection_state().state != "testing"


@pytest.mark.integration
@pytest.mark.parametrize(
    "status,detail,code",
    [
        (401, "opaque-private-credential", "authentication"),
        (404, "opaque-private-credential", "model"),
        (403, "opaque-private-credential", "permission"),
        (429, "insufficient quota opaque-private-credential", "quota"),
        (429, "rate exceeded opaque-private-credential", "rate_limit"),
        (400, "invalid parameter opaque-private-credential", "parameters"),
        (503, "opaque-private-credential", "service"),
    ],
)
def test_failures_are_actionable_without_echoing_errors(
    configured, observed_sdk, status, detail, code
):
    with ErrorTransport(status=status, body={"error": {"message": detail}}) as service:
        use_error_service(service)
        result = connections.verify_model_connection()
        assert len(service.requests) == 1
        assert service.requests[0]["json"]["model"] == "deepseek-flash"
    errors, finished = observed_sdk
    assert finished.is_set() and len(errors) == 1
    assert errors[0].status_code == status
    assert result.state == "failed" and result.code == code
    assert "opaque-private-credential" not in json.dumps(asdict(result))
    assert connections.read_connection_state().code == code


@pytest.mark.integration
def test_supported_keyless_connection_is_not_blocked(configured):
    capacity = template_capacity(CONNECTION_TEMPLATES[2])
    apply_global_config_patch(
        GlobalConfigPatchRequest(
            config={
                "model": "ollama_chat/qwen3",
                "model_capacity": {**capacity, "applies_to_model": "ollama_chat/qwen3"},
            },
            api_key=None,
        )
    )
    assert connections.read_connection_state().state == "unverified"
    apply_global_config_patch(GlobalConfigPatchRequest(config={"model": "gpt-5.4"}))
    state = connections.read_connection_state()
    assert state.state == "missing" and "API Key" in state.message


@pytest.mark.integration
def test_unknown_errors_and_corrupt_records_do_not_return_input(configured):
    failure = connection_failure(RuntimeError("opaque-private-credential"))
    assert "opaque-private-credential" not in failure.message and not failure.stops_batch
    candidate = connections.capture_connection()
    atomic_write_json(connections._record_path(candidate), {"state": "passed", "message": "secret"})
    assert connections.connection_state(candidate).state == "unverified"


@pytest.mark.live
def test_live_connection_uses_production_adapter_and_reuses_record(
    configured, tmp_path, monkeypatch, live_llm
):
    live_llm.install(monkeypatch)
    apply_global_config_patch(
        GlobalConfigPatchRequest(
            api_key=live_llm.credentials.api_key, openai_api_base=live_llm.credentials.base_url
        )
    )
    before = len(live_llm.calls)
    result = connections.verify_model_connection(timeout=60)
    assert result.state == "passed", result
    assert result.capabilities == connections.CAPABILITIES
    sent = live_llm.calls[before:]
    assert len(sent) == 2
    assert all(call["model"] == "deepseek/deepseek-flash" for call in sent)
    assert all(call["max_tokens"] <= 512 and call["num_retries"] == 0 for call in sent)
    assert all(len(json.dumps(call["messages"])) < 4000 for call in sent)
    assert any(tool["function"]["name"] == "connection_check" for tool in sent[0]["tools"])
    assert any(
        message["role"] == "tool" and "OK" in message["content"] for message in sent[1]["messages"]
    )
    kb = tmp_path / "inherits-verified"
    initialize_kb(kb, seed_environment=False)
    assert connections.read_connection_state(kb).state == "passed"
    assert len(live_llm.calls) - before == 2


@pytest.mark.integration
def test_sdk_log_messages_do_not_echo_credentials(configured, caplog):
    from ultrakb.model_log_secrets import redact_model_logs

    # Literal diagnostics test the log boundary, never pretend a probe succeeded.
    with redact_model_logs(connections.capture_connection().credentials):
        logging.getLogger("openai.agents").error("Provider echoed %s", "opaque-private-credential")
    assert "opaque-private-credential" not in caplog.text
    assert "[凭证已隐藏]" in caplog.text


@pytest.mark.integration
def test_duplicate_in_flight_connection_does_not_probe_again(configured, observed_sdk):
    release = threading.Event()
    results = []
    with ErrorTransport(status=401, gate=release) as service:
        use_error_service(service)
        worker = threading.Thread(
            target=lambda: results.append(connections.verify_model_connection(timeout=5)),
            daemon=True,
        )
        worker.start()
        try:
            assert service.wait_for_requests(1)
            assert connections.verify_model_connection().state == "testing"
            assert len(service.requests) == 1
        finally:
            release.set()
            worker.join(6)
        assert not worker.is_alive() and len(service.requests) == 1
    errors, finished = observed_sdk
    assert finished.is_set() and len(errors) == 1
    assert results[0].state == "failed" and results[0].code == "authentication"
    assert connections.read_connection_state().state == "failed"


@pytest.mark.integration
def test_text_only_retained_proof_does_not_claim_tool_capability(configured):
    retained_connection_proof(capabilities=("文本响应",))
    assert connections.read_connection_state().state == "unverified"
    assert connection_failure(connections.ConnectionCapabilityError()).code == "capability"
