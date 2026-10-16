"""A model budget, legacy migration and index recovery share the real request seam."""

import json

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


def test_legacy_preset_migration_preserves_an_auditable_receipt_and_is_idempotent(tmp_path):
    # Exact legacy factory output, retained here rather than calling the new factory.
    from tokenizers import Tokenizer, models, pre_tokenizers

    from ultrakb.capacity_migration import migrate_settings
    from ultrakb.locks import kb_ingest_lock

    tokenizer = Tokenizer(
        models.BPE(
            vocab={c: i for i, c in enumerate(sorted(pre_tokenizers.ByteLevel.alphabet()))},
            merges=[],
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=False)
    asset = tmp_path / "utf8-bound-v1.json"
    asset.write_text(tokenizer.to_str())
    kb = tmp_path / "kb"
    root = kb / ".ultrakb"
    root.mkdir(parents=True)
    from ultrakb.kb_format import KB_FORMAT

    (root / "format.json").write_text(json.dumps(KB_FORMAT))
    old = {
        "model": "deepseek/deepseek-flash",
        "model_capacity": {
            "max_input_tokens": 65536,
            "tokenizer_model": "local:" + str(asset),
            "applies_to_model": "deepseek/deepseek-flash",
        },
    }
    import yaml

    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(old))
    with kb_ingest_lock(root):
        assert migrate_settings(kb, base_url="https://api.deepseek.com")
        updated = path.read_bytes()
        assert not migrate_settings(kb, base_url="https://api.deepseek.com")
    assert path.read_bytes() == updated
    current = yaml.safe_load(updated)
    assert set(current) == set(old)
    assert "tokenizer_model" not in current["model_capacity"]
    assert current["model_capacity"]["context_window_tokens"] > 65536
    receipts = list((root / "capacity-migrations").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["before"] == old["model_capacity"]
    assert asset.is_file()


def test_explicit_user_byte_guard_is_not_migrated(tmp_path):
    import yaml

    from ultrakb.capacity_migration import migrate_settings
    from ultrakb.locks import kb_ingest_lock

    root = tmp_path / ".ultrakb"
    root.mkdir()
    from ultrakb.kb_format import KB_FORMAT

    (root / "format.json").write_text(json.dumps(KB_FORMAT))
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump({"model_capacity": {"max_input_bytes": 65536}}))
    before = path.read_bytes()
    with kb_ingest_lock(root):
        assert not migrate_settings(tmp_path, base_url="https://api.deepseek.com")
    assert path.read_bytes() == before


def test_resume_keeps_paid_calls_when_only_catalog_source_flags_changed(kb_dir):
    from ultrakb.llm_execution import CompletionExecutor, LlmCredentialBundle, RoleBindings
    from ultrakb.model_task import TaskJournal

    def executor(forced):
        return CompletionExecutor(
            RoleBindings(
                "openai/fixture",
                LlmCredentialBundle(),
                model_table={"sha256": "same-exact-catalog", "is_env_forced": forced},
            )
        )

    first = TaskJournal(kb_dir, executor(False))
    first.save({"used": 7, "stages": {"document_index": 7}})
    resumed = executor(True)
    journal = TaskJournal(kb_dir, resumed, resume=first.record.task_id)
    assert resumed.calls == 7
    assert journal.record.used == 7
    assert journal.record.parent_task_id == first.record.task_id


def test_capacity_changes_do_not_change_normalized_original_identity(kb_dir):
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.normalization import normalization_fingerprint

    before = normalization_fingerprint(kb_dir)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model_capacity": {"max_input_tokens": 123456}}
        ),
    )
    assert normalization_fingerprint(kb_dir) == before


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


def test_budget_sdk_release_reuses_the_previous_validated_index(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ultrakb import index_checkpoint
    from ultrakb.llm_execution import CompletionExecutor, LlmCredentialBundle, RoleBindings

    source = tmp_path / "source.pdf"
    source.write_bytes(b"Retained input bytes")
    normalized = SimpleNamespace(
        source_revision_id="a" * 32,
        fingerprint=json.dumps(
            {
                "index_model": "openai/fixture",
                "index_sdk": "0.3.0.dev3+urltrakb.13",
            }
        ),
    )
    executor = CompletionExecutor(RoleBindings("openai/fixture", LlmCredentialBundle()))
    monkeypatch.setattr(
        index_checkpoint,
        "version",
        lambda name: ("0.3.0.dev3+urltrakb.13" if name == "pageindex" else "same-storage"),
    )
    previous = index_checkpoint._identities(normalized, source, executor)[0]
    monkeypatch.setattr(
        index_checkpoint,
        "version",
        lambda name: ("0.3.0.dev3+urltrakb.14" if name == "pageindex" else "same-storage"),
    )
    assert previous in index_checkpoint._identities(normalized, source, executor)


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
