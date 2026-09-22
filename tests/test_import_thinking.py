"""Disabled drafting retains independent reasoning for review and correction."""

import json

import pytest

from openkb.application.documents import import_document
from openkb.application.settings import apply_kb_config_patch, read_kb_config
from openkb.application.settings_data import KbConfigPatchRequest
from tests.http_model_fixture import evidence_response


@pytest.mark.parametrize("planning_thinking", [None, "enabled"])
def test_disabled_import_preserves_stage_controls_and_invalidates_changed_correction(
    kb_dir, tmp_path, model_service, planning_thinking
):
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "compilation_thinking": "disabled",
                "compilation_reasoning_effort": "high",
                **({"planning_thinking": planning_thinking} if planning_thinking else {}),
                "verification_thinking": "enabled",
                "verification_reasoning_effort": "high",
                "correction_thinking": "enabled",
            },
        ),
    )
    original = "Use port 9342 only on version 2."
    unsupported = "Use port 9342 on any version."
    source = tmp_path / "conditional-port.md"
    source.write_text(original)

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        result = evidence_response(payload)
        if payload["stage"] == "generation":
            result["content"] = original if payload.get("revision") else unsupported
        elif payload["stage"] == "verification" and unsupported in payload["candidate"]["content"]:
            return {
                "verdict": "unsupported",
                "reason": "The version 2 condition was removed.",
                "issues": [
                    {
                        "kind": "claim",
                        "candidate": unsupported,
                        "occurrences": ["e1"],
                        "reason": "Restore the required version 2 condition.",
                    }
                ],
            }
        return result

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    content = next((kb_dir / "wiki/concepts").glob("*.md")).read_text()
    assert original in content and unsupported not in content
    assert read_kb_config(kb_dir).compilation_reasoning_effort == "high"
    operations = []
    for body in model_service:
        request = json.loads(body["messages"][-1]["content"])
        stage = request["stage"]
        correction = stage == "generation" and bool(request.get("revision"))
        review = stage == "verification"
        planning = stage == "planning" and planning_thinking == "enabled"
        if correction or review or planning:
            assert body["thinking"] == {"type": "enabled"}
            assert body["reasoning_effort"] == "high"
        else:
            assert body["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in body
        operations.append("correction" if correction else stage)
    assert {"planning", "generation", "verification", "correction"} <= set(operations)
    before = len(model_service)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"compilation_reasoning_effort": "low"}),
    )
    changed = import_document(kb_dir, source)
    assert changed.knowledge_compilation == "completed", changed
    requests = [
        (body, json.loads(body["messages"][-1]["content"])) for body in model_service[before:]
    ]
    corrections = [
        body
        for body, payload in requests
        if payload["stage"] == "generation" and payload.get("revision")
    ]
    assert corrections, "A changed inherited correction effort must invalidate verified pages"
    assert all(body["reasoning_effort"] == "low" for body in corrections)
    if planning_thinking == "enabled":
        plans = [body for body, payload in requests if payload["stage"] == "planning"]
        assert plans, "A changed inherited planning effort must invalidate the saved plan"
        assert all(body["reasoning_effort"] == "low" for body in plans)
