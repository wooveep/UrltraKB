"""Material errors block; noncritical review notices survive publication and reuse."""

import json

import pytest

from openkb.agent.evidence_verifier import verify_content
from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from openkb.processing import ProcessingIncomplete
from tests.http_model_fixture import evidence_response
from tests.test_generation_scopes import payload


@pytest.mark.parametrize("verdict", ["supported", "unsupported", "uncertain"])
def test_original_position_is_reviewed_and_cannot_override_semantic_rejection(
    kb_dir, tmp_path, model_service, verdict
):
    source = tmp_path / "scoped.md"
    source.write_text("# Standby node\n\n## Reinstallation\n\nRestart the local service.")
    reviewed = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        result = evidence_response(request)
        if request["stage"] == "generation":
            result["content"] = "Restart the local service."
        elif request["stage"] == "verification":
            reviewed.append(request)
            return {"verdict": verdict, "reason": "Controlled semantic decision."}
        return result

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed"
    assert len(reviewed) == 1
    evidence = json.dumps(reviewed[0]["evidence"], ensure_ascii=False)
    assert "Standby node" in evidence and "Reinstallation" in evidence
    assert reviewed[0]["candidate"]["content"].strip() == "Restart the local service."
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    if verdict == "supported":
        assert len(pages) == 1 and "Restart the local service." in pages[0].read_text()
    else:
        assert not pages
        assert any(row["reason"] == "knowledge_evidence_mismatch" for row in result.omissions)
    before = len(model_service)
    resumed = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert resumed.knowledge_compilation == "completed"
    assert len(model_service) == before


def test_advisory_publishes_without_repair_and_is_reused(kb_dir, tmp_path, model_service):
    source = tmp_path / "review.md"
    source.write_text("The listener uses port 9342. Its display label is Metrics.")
    calls = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        calls.append(request["stage"])
        if request["stage"] == "verification":
            return {
                "verdict": "advisory",
                "reason": "No material error; retain the noncritical notice.",
                "issues": [],
            }
        result = evidence_response(request)
        if request["stage"] == "generation":
            result["content"] = "The listener uses port 9342."
        return result

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert not result.omissions
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert len(pages) == 1 and "The listener uses port 9342." in pages[0].read_text()
    assert calls.count("generation") == calls.count("verification") == 1
    before = list(calls)
    duplicate = import_document(kb_dir, source)
    assert duplicate.status == "skipped"
    assert duplicate.coverage == result.coverage
    assert calls == before


def test_generic_critical_uncertainty_is_not_rerolled(monkeypatch):
    from openkb.agent import compiler

    calls = []

    def review(*args, **kwargs):
        calls.append(args)
        return json.dumps({"verdict": "uncertain", "reason": "The required version is unknown."})

    monkeypatch.setattr(compiler, "_llm_call", review)
    p = payload()
    result = verify_content(
        "Task",
        "Body",
        p["facts"],
        p["evidence"],
        {"model": "test", "processing": {"max_attempts": 8}},
    )
    assert result["verdict"] == "uncertain"
    assert len(calls) == 1


@pytest.mark.parametrize("damage", ["refs", "kind", "claim", "unmarked", "missing"])
def test_invalid_or_blocking_advisory_never_authorizes_publication(monkeypatch, damage):
    from openkb.agent import compiler

    advisory = {
        "kind": "uncertainty",
        "candidate": "Body",
        "occurrences": ["e1"],
        "reason": "Noncritical wording.",
    }
    response = {"verdict": "advisory", "reason": "Check", "advisories": [advisory]}
    if damage == "refs":
        advisory["occurrences"] = ["unknown"]
    elif damage == "kind":
        advisory["kind"] = []
    elif damage == "claim":
        response["issues"] = [
            {
                "kind": "claim",
                "candidate": "Body",
                "occurrences": ["e1"],
                "reason": "The number is wrong.",
            }
        ]
    elif damage == "unmarked":
        response["verdict"] = "supported"
    else:
        response["advisories"] = []
    calls = []

    def review(*args, **kwargs):
        calls.append(args)
        return json.dumps(response)

    monkeypatch.setattr(compiler, "_llm_call", review)
    p = payload()
    with pytest.raises(ProcessingIncomplete, match="evidence_verification_invalid"):
        verify_content(
            "Task",
            "Body",
            p["facts"],
            p["evidence"],
            {"model": "test", "processing": {"max_attempts": 8}},
        )
    assert len(calls) == 2


def test_independent_planned_pages_keep_separate_review_decisions(kb_dir, tmp_path, model_service):
    import yaml

    config = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config.read_text())
    settings["processing"].update(concurrency=2, context_tokens=16384, output_tokens=2048)
    config.write_text(yaml.safe_dump(settings))
    source = tmp_path / "related.md"
    source.write_text("# Listener\n\nAlpha uses port 9342.\n\nBeta uses port 9343.")
    reviewed = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        value = evidence_response(request)
        if request["stage"] == "planning":
            value = {
                "overview": {"text": "Listener ports.", "ranges": [[0, 3]], "limitations": []},
                "page_changes": [
                    {
                        "local_key": "alpha",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/alpha",
                        "title": "Alpha",
                        "purpose": "Alpha listener port.",
                        "subject_ranges": [[0, 2]],
                        "necessary_context": [],
                    },
                    {
                        "local_key": "beta",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/beta",
                        "title": "Beta",
                        "purpose": "Beta listener port.",
                        "subject_ranges": [[2, 3]],
                        "necessary_context": [],
                    },
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        elif request["stage"] == "generation":
            title = request["page"]["title"]
            value = {
                "content": f"{title} uses port {'9342' if title == 'Alpha' else '9343'}.",
                "covered": [row["id"] for row in request["occurrences"]],
            }
        elif request["stage"] == "verification":
            title = request["page"]["title"]
            reviewed.append(title)
            value = {
                "verdict": "supported" if title == "Alpha" else "uncertain",
                "reason": "Supported original."
                if title == "Alpha"
                else "Required scope is unresolved.",
            }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert sorted(reviewed) == ["Alpha", "Beta"]
    assert (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert any(row["reason"] == "knowledge_evidence_mismatch" for row in result.omissions)
    assert not any(
        json.loads(row["messages"][-1]["content"])["stage"] == "verification_batch"
        for row in model_service
    )
