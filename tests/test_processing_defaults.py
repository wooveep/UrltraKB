"""Legacy knowledge bases can compile without configuring execution budgets."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from openkb import config
from openkb.application.settings import (
    apply_global_config_patch,
    apply_kb_config_patch,
    read_settings_view,
)
from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
from openkb.locks import atomic_write_json, atomic_write_text
from openkb.processing import RequestLimits
from tests.http_model_fixture import evidence_response


@pytest.fixture
def legacy_kb(kb_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global/global.yaml")
    atomic_write_text(kb_dir / ".openkb/config.yaml", "model: gpt-5.4\nlanguage: en\n")
    return kb_dir


@pytest.mark.parametrize("explicit_null", [False, True])
def test_legacy_recompile_reaches_review_without_manual_budgets(
    legacy_kb, monkeypatch, explicit_null
):
    import litellm

    from openkb.application.recompilation import recompile_document

    if explicit_null:
        atomic_write_text(
            legacy_kb / ".openkb/config.yaml",
            "model: gpt-5.4\nlanguage: en\nprocessing: null\n",
        )
    atomic_write_json(
        legacy_kb / ".openkb/hashes.json", {"legacy": {"doc_name": "notes", "type": "md"}}
    )
    atomic_write_text(legacy_kb / "wiki/sources/notes.md", "# Notes\nOriginal knowledge.")
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            evidence_response(json.loads(kwargs["messages"][-1]["content"]))
                        )
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(litellm, "completion", completion)
    result = asyncio.run(recompile_document(legacy_kb, "legacy"))
    assert result.message == "needs_acceptance", result
    assert result.document.parse_id is not None
    assert result.document.usage["observable_attempts"] == len(calls) == 5
    assert [json.loads(call["messages"][-1]["content"])["stage"] for call in calls[:2]] == [
        "index_structure",
        "index_summary",
    ]
    assert all(call["max_tokens"] > 0 and call["timeout"] > 0 for call in calls)
    # Resolving defaults must not pin an override into the legacy library.
    saved = yaml.safe_load((legacy_kb / ".openkb/config.yaml").read_text())
    assert saved.get("processing") is None


def test_default_budgets_are_visible_overridable_and_restored_on_clear(legacy_kb):
    default = read_settings_view(legacy_kb)
    limits = default.values.processing
    # A bare processing map is not a safe model contract; make the test's
    # direct construction explicit rather than relying on a generic context.
    RequestLimits.from_config(
        {
            "processing": {
                **limits,
                "context_tokens": 32_768,
                "max_context_tokens": 32_768,
            }
        }
    )
    assert limits["concurrency"] == 2
    assert default.sources["processing"] == "default"
    assert read_settings_view().values.processing == limits
    global_limits = {**limits, "max_requests": 17, "concurrency": 4}
    apply_global_config_patch(GlobalConfigPatchRequest(config={"processing": global_limits}))
    inherited = read_settings_view(legacy_kb)
    assert inherited.values.processing == global_limits
    assert inherited.sources["processing"] == "global"
    local_limits = {**limits, "max_requests": 9, "concurrency": 1}
    apply_kb_config_patch(
        legacy_kb, KbConfigPatchRequest(kb=str(legacy_kb), config={"processing": local_limits})
    )
    assert read_settings_view(legacy_kb).values.processing == local_limits
    apply_kb_config_patch(
        legacy_kb, KbConfigPatchRequest(kb=str(legacy_kb), config={"processing": None})
    )
    assert read_settings_view(legacy_kb).values.processing == global_limits
    apply_global_config_patch(GlobalConfigPatchRequest(config={"processing": None}))
    cleared = read_settings_view(legacy_kb)
    assert cleared.values.processing == limits
    assert cleared.sources["processing"] == "default"
    assert read_settings_view().values.processing == limits


def test_resolved_default_budgets_do_not_leak_changes_between_reads(legacy_kb):
    defaults = deepcopy(config.DEFAULT_CONFIG)
    first, _ = config.resolve_effective_config(legacy_kb)
    first["processing"]["max_requests"] = 1
    second, _ = config.resolve_effective_config(legacy_kb)
    assert second["processing"]["max_requests"] == defaults["processing"]["max_requests"]
    loaded = config.load_config(legacy_kb / ".openkb/config.yaml")
    loaded["processing"]["max_tokens"] = 1
    assert config.DEFAULT_CONFIG == defaults


def test_invalid_explicit_budget_is_not_replaced_by_defaults(legacy_kb, monkeypatch):
    import litellm

    from openkb.application.documents import import_document

    monkeypatch.setattr(litellm, "completion", lambda **_: pytest.fail("Invalid budget used"))
    atomic_write_text(
        legacy_kb / ".openkb/config.yaml",
        "model: openai/offline-test\nprocessing: {}\n",
    )
    source = legacy_kb / "input.md"
    source.write_text("Original knowledge.", encoding="utf-8")
    result = import_document(legacy_kb, source)
    assert result.status == "unfinished"
    assert result.stage == "configuration"
    assert result.reason == "model_capabilities_required"
    assert result.source_intake == "saved"


def test_settings_validate_capacity_against_the_pending_model_and_endpoint(legacy_kb, monkeypatch):
    from openkb.model_capabilities import ModelCapabilities

    seen = []

    def capabilities(model, endpoint):
        seen.append((model, endpoint))
        return (
            ModelCapabilities(8192, None, 2048, True)
            if model == "known-at-endpoint" and endpoint == "https://models.example.test/v1"
            else None
        )

    monkeypatch.setattr("openkb.processing_limits.selected_model_capabilities", capabilities)
    automatic = {**config.DEFAULT_CONFIG["processing"], "output_tokens": 1024}
    apply_kb_config_patch(
        legacy_kb,
        KbConfigPatchRequest(
            kb=str(legacy_kb),
            config={"model": "known-at-endpoint", "processing": automatic},
            openai_api_base="https://models.example.test/v1",
        ),
    )
    assert seen[-1] == ("known-at-endpoint", "https://models.example.test/v1")

    before = (legacy_kb / ".openkb/config.yaml").read_text()
    with pytest.raises(ValueError, match="unknown model"):
        apply_kb_config_patch(
            legacy_kb,
            KbConfigPatchRequest(
                kb=str(legacy_kb),
                config={"model": "unknown-at-endpoint", "processing": automatic},
            ),
        )
    assert (legacy_kb / ".openkb/config.yaml").read_text() == before

    independent = {
        **automatic,
        "input_tokens": 4096,
        "max_input_tokens": 8192,
        "max_output_tokens": 2048,
        "shared_context": False,
    }
    apply_kb_config_patch(
        legacy_kb,
        KbConfigPatchRequest(
            kb=str(legacy_kb),
            config={"model": "unknown-at-endpoint", "processing": independent},
        ),
    )
    assert read_settings_view(legacy_kb).values.processing == independent


def test_known_shared_model_cannot_be_redeclared_as_independent(monkeypatch):
    from openkb.model_capabilities import ModelCapabilities
    from openkb.processing import ProcessingIncomplete

    monkeypatch.setattr(
        "openkb.processing_limits.selected_model_capabilities",
        lambda *_: ModelCapabilities(8192, None, 2048, True),
    )

    with pytest.raises(ProcessingIncomplete, match="model_capabilities_required"):
        RequestLimits.from_config(
            {
                "model": "shared-model",
                "processing": {
                    **config.DEFAULT_CONFIG["processing"],
                    "input_tokens": 100_000,
                    "max_input_tokens": 100_000,
                    "output_tokens": 1024,
                    "max_output_tokens": 1024,
                    "shared_context": False,
                },
            }
        )
