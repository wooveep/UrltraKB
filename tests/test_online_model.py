"""Configuration contracts for the explicit real-provider test entry point."""

import json
import os

import pytest

from tests.online_model import load_online_model


def _profile(tmp_path, name, key):
    kb = tmp_path / name
    (kb / ".openkb").mkdir(parents=True)
    (kb / ".openkb/config.yaml").write_text(
        f"model: openai/{name}\nplanning_thinking: disabled\n", encoding="utf-8"
    )
    (kb / ".env").write_text(
        f"LLM_API_KEY={key}\nOPENAI_API_BASE=https://provider.invalid/v1\n", encoding="utf-8"
    )
    return kb


def test_explicit_profile_keeps_model_and_credentials_together(tmp_path, monkeypatch):
    first = _profile(tmp_path, "first", "private-first")
    second = _profile(tmp_path, "second", "private-second")
    monkeypatch.setenv("OPENKB_TEST_MODEL_KB", str(first))
    before = dict(os.environ)
    a, b = load_online_model(), load_online_model(second)
    assert (a.settings["model"], a.bundle.api_key) == ("openai/first", "private-first")
    assert (b.settings["model"], b.bundle.api_key) == ("openai/second", "private-second")
    assert dict(os.environ) == before
    visible = repr(a) + json.dumps(a.description())
    assert "private-first" not in visible and "provider.invalid" not in visible


def test_online_setup_requires_explicit_existing_kb(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENKB_TEST_MODEL_KB", raising=False)
    with pytest.raises(ValueError, match="OPENKB_TEST_MODEL_KB"):
        load_online_model()
    with pytest.raises(ValueError, match="config.yaml"):
        load_online_model(tmp_path)


def test_pages_comparison_changes_only_rules_and_preserves_literal_data():
    from openkb.agent.document_protocol import plan_messages
    from tests.online_step3_comparison import comparison_messages

    messages = plan_messages(
        {"source_id": "a" * 32, "version_id": "b" * 64, "parse_id": "c" * 64},
        {"overview": {"text": 'Overview with {overview} and "task_rules": text'}},
        {"kind": "global_pages"},
        [],
        "",
        ["product"],
        "",
        planning_context={"navigation_style": "legacy_pdf"},
    )
    body = json.loads(messages[-1]["content"])
    body["task_rules"] = "Previous rules"
    messages[-1]["content"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    saved = {"messages": list(messages), "inverse": dict(messages.inverse)}
    baseline, candidate = comparison_messages(saved)
    assert baseline == messages and baseline.inverse == candidate.inverse == messages.inverse
    old, new = (json.loads(row[-1]["content"]) for row in (baseline, candidate))
    assert old.pop("task_rules") != new.pop("task_rules")
    assert old == new
    assert baseline[0] == candidate[0]
    assert (
        baseline[-1]["content"].rsplit(',"task_rules":', 1)[0]
        == (candidate[-1]["content"].rsplit(',"task_rules":', 1)[0])
    )
    body["carry"]["pages"] = [{"title": "Already selected"}]
    saved["messages"][-1]["content"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(ValueError, match="initial global pages request"):
        comparison_messages(saved)


def test_missing_credentials_fail_before_dispatch(tmp_path, monkeypatch):
    from openkb import config

    kb = _profile(tmp_path, "missing", "")
    global_dir = tmp_path / "global"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", global_dir)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_dir / "global.yaml")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="credentials are unavailable"):
        load_online_model(kb)


def test_pages_comparison_uses_production_navigation_identity(kb_dir, tmp_path, monkeypatch):
    from openkb.agent.document_global_context import freeze_context, messages_for
    from openkb.processing import RequestLimits
    from tests.online_step3 import Audit
    from tests.online_step3_comparison import compare_pages
    from tests.test_document_markdown_planning import SETTINGS
    from tests.test_document_orchestrator import _DummyParsed, _DummySource

    source, parsed = _DummySource(), _DummyParsed(2)
    navigation = {"id": "persisted-navigation-record", "nodes": []}
    profile = load_online_model(_profile(tmp_path, "comparison", "private-comparison"))
    profile.settings["processing"] = SETTINGS["processing"]
    limits = RequestLimits.from_config(profile.settings)
    state = {
        "pages": [],
        "overview_snapshot": {"text": "Saved overview", "partial": False},
        "tasks": {},
        "deferred_suggestions": [],
    }
    snapshot = freeze_context(
        state, navigation, source, parsed, [], profile.settings, limits, [], "", []
    )
    messages = messages_for(
        snapshot, snapshot["nodes"], state, source, parsed, profile.settings, limits, [], "", []
    )
    path = tmp_path / "saved-request.json"
    path.write_text(json.dumps({"messages": messages, "inverse": dict(messages.inverse)}))
    observed = []
    monkeypatch.setattr(
        "openkb.agent.document_markdown_planner._call",
        lambda *args: observed.append(args) or "## Create pages\nNone\n## Update pages\nNone",
    )
    monkeypatch.setattr("openkb.pageindex_store.indexed_reader", lambda *args: object())
    audit = Audit(tmp_path, profile.bundle.api_key)
    compare_pages(path, audit, profile, profile.settings, kb_dir, source, parsed, navigation)
    assert len(observed) == 4
    assert all(args[3] == profile.bundle and args[5] == "pages" for args in observed)
    for label in ("baseline-1", "candidate-1", "candidate-2", "baseline-2"):
        result = json.loads((tmp_path / f"comparison-{label}-accepted.json").read_text())
        assert result["no_pages"] and not result["pages"] and not result["truncated"]
    assert audit.phase == "step3"


@pytest.mark.parametrize("markdown", [False, True])
def test_online_audit_records_real_receipt_without_global_profiler(
    kb_dir, tmp_path, model_service, markdown
):
    import sys

    from openkb.agent import compiler
    from openkb.agent.evidence_wire import WireMessages
    from openkb.config import load_config, resolve_credential_bundle
    from openkb.processing import processing_scope
    from tests.http_model_fixture import ModelReply
    from tests.online_step3 import Audit

    expected = "# Overview\n\nAvailable navigation." if markdown else '{"sections": []}'
    model_service.respond = lambda _: ModelReply(expected)
    settings = load_config(kb_dir / ".openkb/config.yaml")
    audit = Audit(tmp_path, "private-test-secret")
    before = sys.getprofile()
    messages = WireMessages(
        [{"role": "user", "content": '{"stage":"index_structure","evidence":{}}'}], {}
    )
    with audit.capture(), processing_scope(settings):
        assert sys.getprofile() is before
        result = compiler._llm_call(
            settings["model"],
            messages,
            "index_structure",
            bundle=resolve_credential_bundle(kb_dir),
            decode_response=not markdown,
        )
    assert sys.getprofile() is before
    assert result == expected
    request = json.loads((tmp_path / "request-01.json").read_text())
    response = json.loads((tmp_path / "response-01.json").read_text())
    assert request["messages"] == messages
    assert response["usage"]["prompt_tokens"] > 0
    assert response["provider_content"] == expected
    assert response["decoded_content"] == expected
    assert response["finish_reason"] == "stop"
    assert len(audit.requests) == 1


def test_online_audit_retains_external_transport_without_claiming_provider_failure(
    kb_dir, tmp_path
):
    from openkb.config import load_config
    from openkb.external_request_usage import external_request_usage
    from openkb.processing import processing_scope
    from tests.online_step3 import Audit

    audit = Audit(tmp_path, "private-test-secret")
    settings = load_config(kb_dir / ".openkb/config.yaml")
    with audit.capture(), processing_scope(settings):
        with external_request_usage(0, "ocr", model_time=False) as receipt:
            receipt["tokens"] = 0
    response = json.loads((tmp_path / "response-01.json").read_text())
    assert "failed" not in response
    assert response["response_observed"] is False
    assert response["measurement"]["transport_complete"] is True
    assert response["measurement"]["operation"] == "ocr"
    assert response["measurement"]["request_seconds"] >= 0
    assert response["usage"] is None


@pytest.mark.parametrize("with_usage", [False, True])
def test_online_production_options_and_unknown_usage(kb_dir, tmp_path, model_service, with_usage):
    from openkb.agent import compiler
    from openkb.agent.source_protocol import source_messages
    from openkb.locks import atomic_write_text
    from openkb.processing import processing_scope
    from tests.online_audit import Audit

    config = kb_dir / ".openkb/config.yaml"
    atomic_write_text(config, config.read_text() + "\ncompilation_thinking: disabled\n")
    model = load_online_model(kb_dir)
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "total_tokens": 130,
        "prompt_tokens_details": {"cached_tokens": 60},
        "completion_tokens_details": {"reasoning_tokens": 10},
    }
    model_service.usage = usage if with_usage else None
    audit = Audit(tmp_path, model.bundle.api_key)
    messages = source_messages({"blocks": []}, {"stage": "index_structure"}, "Return sections.")
    with audit.capture(), processing_scope(model.settings):
        compiler._llm_call(
            model.settings["model"],
            messages,
            "index_structure",
            bundle=model.bundle,
            **model.model_options(),
        )
    assert model_service[-1]["thinking"] == {"type": "disabled"}
    request = json.loads((tmp_path / "request-01.json").read_text())
    response = json.loads((tmp_path / "response-01.json").read_text())
    assert request["options_semantics"] == "safe_display_projection_not_sdk_replay_parameters"
    assert model.bundle.api_key not in json.dumps(request)
    measured = response["measurement"]
    assert measured["input_tokens"] == (100 if with_usage else None)
    assert measured["output_tokens"] == (30 if with_usage else None)
    assert measured["cache_read_tokens"] == (60 if with_usage else None)
    assert measured["cache_miss_tokens"] == (40 if with_usage else None)
    assert measured["reasoning_tokens"] == (10 if with_usage else None)
