"""Dependency-only upgrades reuse unchanged candidate decisions before new joint review."""

import json

import pytest

from openkb.agent import evidence_checkpoints
from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response

# Portable code fingerprint of dependency_scope at 6408203. The first run uses
# current behavior but old generation/topic key identity; it is not an old build replay.
PREVIOUS_SCOPE = "dbf7c177906b3d45cd0d34ae87dbf8276dc8da45cb064d6d49e0a4a35ac302f0"


@pytest.mark.parametrize("verdict", ["supported", "unsupported", "uncertain"])
def test_dependency_upgrade_reuses_unchanged_generation_and_negative_review(
    kb_dir, tmp_path, model_service, monkeypatch, verdict
):
    source = tmp_path / "approval.md"
    source.write_text("Proceed only after approval.")
    candidate = (
        "Proceed without an approval."
        if verdict == "unsupported"
        else "Proceed only after approval."
    )
    verdicts = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "generation":
            if "fragments" in value:
                for fragment in value["fragments"]:
                    fragment["content"] = candidate
            else:
                value["content"] = candidate
        elif payload["stage"] == "verification":
            verdicts.append(verdict)
            value = {"verdict": verdict, "reason": "The original approval condition decides."}
            if verdict == "unsupported":
                value["issues"] = [
                    {
                        "kind": "claim",
                        "candidate": candidate,
                        "occurrences": [payload["occurrences"][0]["id"]],
                        "reason": "The original requires approval before proceeding.",
                    }
                ]
        return value

    model_service.respond = respond
    revision = evidence_checkpoints.module_revision
    queried_old = []

    def previous_identity(name, **kwargs):
        if name == "openkb.agent.dependency_scope":
            queried_old.append(name)
            return PREVIOUS_SCOPE
        return revision(name, **kwargs)

    assert revision("openkb.agent.dependency_scope") != PREVIOUS_SCOPE
    with monkeypatch.context() as previous:
        previous.setattr(evidence_checkpoints, "module_revision", previous_identity)
        first = import_document(kb_dir, source)
    assert queried_old and verdict in verdicts
    assert first.knowledge_compilation == "completed", first
    assert (kb_dir / "wiki/concepts/notes.md").exists() == (verdict == "supported")
    before = len(model_service)
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.status == "added" and resumed.knowledge_compilation == "completed", resumed
    assert (kb_dir / "wiki/concepts/notes.md").exists() == (verdict == "supported")
    assert not {
        json.loads(call["messages"][-1]["content"])["stage"] for call in model_service[before:]
    } & {"generation", "verification", "verification_batch"}
    assert first.omissions == resumed.omissions
