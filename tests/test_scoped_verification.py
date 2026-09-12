"""Bounded, located review feedback cannot silently relax publication checks."""

import json

import pytest

from openkb.processing import ProcessingIncomplete
from tests.test_generation_scopes import payload


def test_verifier_sees_source_identity_but_not_extractor_claims():
    from openkb.agent.evidence_verifier import verification_payload

    p = payload()
    v = verification_payload("GPU configuration", "Original text", p["facts"], p["evidence"])
    assert v["source_scopes"] == p["source_scopes"]
    assert "statement" not in v["facts"][0]
    assert v["evidence"] == p["evidence"]


def test_invalid_issue_location_never_accepts_a_supported_review(monkeypatch):
    from openkb.agent import compiler
    from openkb.agent.evidence_verifier import verify_content

    replies = []

    def call(*args, **kwargs):
        replies.append(args)
        return json.dumps(
            {
                "verdict": "supported",
                "reason": "OK",
                "issues": [
                    {
                        "kind": "scope",
                        "candidate": "invented candidate",
                        "occurrences": ["unknown"],
                        "reason": "Invalid reference",
                    }
                ],
            }
        )

    monkeypatch.setattr(compiler, "_llm_call", call)
    p = payload()
    with pytest.raises(ProcessingIncomplete, match="evidence_verification_invalid"):
        verify_content(
            "GPU",
            "Original",
            p["facts"],
            p["evidence"],
            {"model": "test", "processing": {"max_attempts": 2}},
        )
    assert len(replies) == 2


def test_claimed_missing_path_is_reassessed_once_with_explicit_evidence(monkeypatch):
    from openkb.agent import compiler
    from openkb.agent.evidence_verifier import verify_content

    requests = []

    def call(model, messages, *args, **kwargs):
        requests.append(json.loads(messages[-1]["content"]))
        if len(requests) == 1:
            return json.dumps(
                {
                    "verdict": "unsupported",
                    "reason": "The path is missing",
                    "issues": [
                        {
                            "kind": "evidence_missing",
                            "candidate": "Dual screen",
                            "occurrences": ["e4"],
                            "path": ["Dual screen"],
                            "reason": "Path absent from supplied evidence",
                        }
                    ],
                }
            )
        return json.dumps(
            {"verdict": "supported", "reason": "The supplied path is present", "issues": []}
        )

    monkeypatch.setattr(compiler, "_llm_call", call)
    p = payload()
    result = verify_content(
        "GPU",
        "Dual screen",
        p["facts"],
        p["evidence"],
        {"model": "test", "processing": {"max_attempts": 2}},
    )
    assert result["verdict"] == "supported"
    assert len(requests) == 2
    assert requests[0]["evidence"] == requests[1]["evidence"]
    assert requests[1]["review_context"]["present_paths"] == [["Dual screen"]]


def test_real_unsupported_dependency_is_not_retried_to_success(monkeypatch):
    from openkb.agent import compiler
    from openkb.agent.evidence_verifier import verify_content

    calls = []

    def call(*args, **kwargs):
        calls.append(args)
        return json.dumps(
            {
                "verdict": "unsupported",
                "reason": "False dependency",
                "issues": [
                    {
                        "kind": "scope",
                        "candidate": "License requires dual screen",
                        "occurrences": ["e4"],
                        "reason": "These are distinct tasks",
                    }
                ],
            }
        )

    monkeypatch.setattr(compiler, "_llm_call", call)
    p = payload()
    result = verify_content(
        "GPU",
        "License requires dual screen",
        p["facts"],
        p["evidence"],
        {"model": "test", "processing": {"max_attempts": 2}},
    )
    assert result["verdict"] == "unsupported" and len(calls) == 1


@pytest.mark.parametrize("final_verdict", ["supported", "unsupported"])
def test_explicit_stronger_review_is_once_per_draft_and_keeps_real_rejections(
    monkeypatch, final_verdict
):
    from openkb.agent import compiler
    from openkb.agent.evidence_verifier import verify_content

    calls = []

    def call(model, messages, operation, **kwargs):
        calls.append((json.loads(messages[-1]["content"]), kwargs))
        return json.dumps(
            {
                "verdict": "unsupported" if len(calls) == 1 else final_verdict,
                "reason": "The independently reviewed scope result",
            }
        )

    monkeypatch.setattr(compiler, "_llm_call", call)
    p = payload()
    from openkb.agent.evidence_generation_protocol import fragment_bindings
    from tests.test_generation_scopes import response

    bindings = fragment_bindings(response(p))
    settings = {
        "model": "test",
        "compilation_thinking": "disabled",
        "verification_adjudication_thinking": "enabled",
        "processing": {"max_attempts": 2},
    }
    result = verify_content(
        "GPU", "Dual screen", p["facts"], p["evidence"], settings, bindings=bindings
    )
    assert result["verdict"] == final_verdict
    assert len(calls) == 2
    assert calls[0][1]["extra_body"]["thinking"]["type"] == "disabled"
    assert calls[1][1]["extra_body"]["thinking"]["type"] == "enabled"
    assert calls[0][0]["evidence"] == calls[1][0]["evidence"]
    assert "review_context" in calls[1][0]
    assert calls[0][0]["fragment_bindings"] == calls[1][0]["fragment_bindings"] == bindings
    assert settings["compilation_thinking"] == "disabled"


def test_adjudication_setting_rechecks_generation_but_reuses_facts_and_plan(
    kb_dir, tmp_path, model_service
):
    import yaml

    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_actions import continue_source
    from openkb.cancellation import OperationCancelled

    source = tmp_path / "adjudication.md"
    source.write_text("The startup limit is 37 seconds.")

    def stop(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    result = import_document(kb_dir, source, context=ExecutionContext(on_event=stop))
    assert result.knowledge_compilation == "stopped"
    count = len(model_service)
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["verification_adjudication_thinking"] = "enabled"
    config_path.write_text(yaml.safe_dump(config))
    result = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert result.knowledge_compilation == "completed"
    assert [json.loads(c["messages"][-1]["content"])["stage"] for c in model_service[count:]] == [
        "generation",
    ]
    # The regenerated body and ordinary review mode are unchanged; its exact
    # supported response is revalidated from the separate review record.


@pytest.mark.parametrize("key", ["verification_adjudication_thinking", "correction_thinking"])
@pytest.mark.parametrize("mode", [True, "auto", [], {}])
def test_invalid_adjudication_mode_is_rejected_at_config_boundary(mode, key):
    from openkb.config import DEFAULT_CONFIG, validate_runtime_config

    with pytest.raises(ValueError, match=key):
        validate_runtime_config({**DEFAULT_CONFIG, key: mode})


@pytest.mark.parametrize("final_verdict", ["supported", "unsupported"])
def test_invalid_normal_reviews_reach_one_explicit_adjudication(monkeypatch, final_verdict):
    from openkb.agent import compiler
    from openkb.agent.evidence_verifier import verify_content

    requests = []

    def call(model, messages, *args, **kwargs):
        requests.append(json.loads(messages[-1]["content"]))
        if len(requests) <= 2:
            return '{"verdict":"unsupported","reason":"Review", "issues":[{"bad":"shape"}]}'
        return json.dumps({"verdict": final_verdict, "reason": "Independent review", "issues": []})

    monkeypatch.setattr(compiler, "_llm_call", call)
    p = payload()
    result = verify_content(
        "GPU",
        "Candidate",
        p["facts"],
        p["evidence"],
        {
            "model": "test",
            "compilation_thinking": "disabled",
            "verification_adjudication_thinking": "enabled",
            "processing": {"max_attempts": 2},
        },
    )
    assert result["verdict"] == final_verdict
    assert len(requests) == 3
    assert requests[-1]["review_context"]["previous_error"] == "evidence_verification_invalid"
    assert requests[0]["content"] == requests[-1]["content"]
    assert requests[0]["evidence"] == requests[-1]["evidence"]
