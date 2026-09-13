"""Configured review effort reaches the provider and invalidates only affected work."""

import json

from openkb.application.documents import import_document
from openkb.application.settings import apply_kb_config_patch, read_kb_config
from openkb.application.settings_data import KbConfigPatchRequest
from tests.http_model_fixture import evidence_response


def test_bounded_stronger_review_is_explicit_measured_and_reusable(kb_dir, tmp_path, model_service):
    controls = {
        "navigation": {"enabled": True},
        "compilation_thinking": "disabled",
        "verification_thinking": "enabled",
        "verification_reasoning_effort": "low",
        "verification_adjudication_reasoning_effort": "high",
    }
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config=controls))
    assert read_kb_config(kb_dir).verification_adjudication_reasoning_effort == "high"
    source = tmp_path / "policy.md"
    source.write_text("The maximum timeout is 30 seconds; retries must not overlap. " * 6)

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification" and body.get("reasoning_effort") == "low":
            return {"verdict": "uncertain", "reason": "This claim needs stronger review."}
        return evidence_response(payload)

    model_service.respond = respond
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    reviews = [
        row
        for row in model_service
        if json.loads(row["messages"][-1]["content"])["stage"] == "verification"
    ]
    assert [row["reasoning_effort"] for row in reviews] == ["low", "low", "high"]
    measured = first.usage["measurement"]["requests"]
    assert any(row["effective_options"].get("reasoning_effort") == "high" for row in measured)
    before = len(model_service)
    assert import_document(kb_dir, source).status == "skipped"
    assert len(model_service) == before
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"verification_reasoning_effort": "high"}),
    )
    changed = import_document(kb_dir, source)
    assert changed.knowledge_compilation == "completed", changed
    assert all(
        json.loads(row["messages"][-1]["content"])["stage"] != "facts"
        for row in model_service[before:]
    )
    assert any(
        json.loads(row["messages"][-1]["content"])["stage"] == "index_summary_verification"
        and row["reasoning_effort"] == "high"
        for row in model_service[before:]
    )
