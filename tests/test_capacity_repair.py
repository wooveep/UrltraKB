"""A model budget, legacy migration and index recovery share the real request seam."""

import pytest

pytestmark = pytest.mark.unit


def test_connection_example_describes_service_capacity_without_a_byte_tokenizer():
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES

    capacity = template_capacity(CONNECTION_TEMPLATES[2])
    assert capacity["context_window_tokens"] > capacity["output_reserve_tokens"]
    assert capacity["measurement"] == "utf8_token_upper_bound"
    assert "max_input_tokens" not in capacity
    assert "tokenizer_model" not in capacity
    assert capacity["provenance"]


def test_complete_request_reserves_the_output_that_will_actually_be_sent():
    from ultrakb.execution_capacity import capacity_policy, require_request_capacity
    from ultrakb.model_tokenizer import ContextCapacityError, TokenMeasure

    policy = capacity_policy(
        {
            "model": "private/deployment",
            "model_capacity": {
                "context_window_tokens": 300,
                "output_reserve_tokens": 20,
                "max_output_tokens": 200,
                "measurement": "utf8_token_upper_bound",
            },
        },
        custom_endpoint=True,
    )
    measure = TokenMeasure(policy)
    payload = {"messages": [{"role": "user", "content": "x" * 80}], "max_tokens": 180}
    assert measure.payload_tokens(payload) < measure.input_limit
    with pytest.raises(ContextCapacityError, match="capacity"):
        require_request_capacity(measure, payload, operation="document_index")


def test_capacity_changes_do_not_change_normalized_original_identity(kb_dir):
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.normalization import normalization_policy

    before = normalization_policy(kb_dir)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model_capacity": {"max_input_tokens": 123456}}
        ),
    )
    assert normalization_policy(kb_dir) == before


def test_missing_retired_factory_asset_can_migrate_without_inventing_a_tokenizer(tmp_path):
    from ultrakb.capacity_migration import legacy_template

    capacity = {
        "applies_to_model": "deepseek/deepseek-flash",
        "max_input_tokens": 65536,
        "tokenizer_model": "local:" + str(tmp_path / "utf8-bound-v1-33f2bfa809d90e0d.json"),
        "measurement": "tokenizer",
        "schema_version": 1,
    }
    assert legacy_template(capacity, "deepseek/deepseek-flash")
    capacity["max_input_tokens"] = 131072
    assert not legacy_template(capacity, "deepseek/deepseek-flash")


def test_private_endpoint_migration_does_not_reinherit_a_global_byte_guard(kb_dir, monkeypatch):
    import yaml

    from ultrakb import config
    from ultrakb.capacity_migration import migrate_settings
    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    global_path = kb_dir / "settings/global.yaml"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_path)
    atomic_write_text(global_path, yaml.safe_dump({"model_capacity": {"max_input_bytes": 65536}}))
    path = kb_dir / ".ultrakb/config.yaml"
    values = yaml.safe_load(path.read_text())
    values.update(
        model="deepseek/deepseek-flash",
        model_capacity={
            "applies_to_model": "deepseek/deepseek-flash",
            "max_input_tokens": 65536,
            "tokenizer_model": "local:/missing/utf8-bound-v1-33f2bfa809d90e0d.json",
        },
    )
    atomic_write_text(path, yaml.safe_dump(values))
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        assert migrate_settings(kb_dir, base_url="https://private.invalid")
    effective, _ = config.resolve_effective_config(kb_dir)
    assert effective["model_capacity"] == {}
    assert capacity_policy(effective, custom_endpoint=True, base_url="https://private.invalid")[
        "unknown_reason"
    ]


def test_actual_smaller_output_reclaims_context_at_the_send_boundary(monkeypatch):
    import litellm

    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.llm_execution import (
        CompletionExecutor,
        LlmCredentialBundle,
        ModelRequest,
        RoleBindings,
    )
    from ultrakb.model_tokenizer import TokenMeasure

    measure = TokenMeasure(
        capacity_policy(
            {
                "model": "openai/fixture",
                "model_capacity": {
                    "context_window_tokens": 1000,
                    "output_reserve_tokens": 400,
                    "measurement": "utf8_token_upper_bound",
                },
            },
            custom_endpoint=True,
        )
    )
    executor = CompletionExecutor(RoleBindings("openai/fixture", LlmCredentialBundle()))
    executor.question_capacity = measure
    sent = []

    def completion(**kwargs):
        sent.append(kwargs)
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {"role": "assistant", "content": "Accepted"},
                    "finish_reason": "stop",
                }
            ]
        )

    monkeypatch.setattr(litellm, "completion", completion)
    request = ModelRequest(
        [{"role": "user", "content": "x" * 650}],
        operation="answer",
        stage="capacity-reclaim",
        generation_options={"max_tokens": 10},
    )
    assert executor.complete("answer", request).text == "Accepted"
    assert len(sent) == 1 and executor._answer_input_tokens > measure.input_limit
