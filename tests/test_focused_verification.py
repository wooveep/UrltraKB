"""Material errors block; noncritical review notices survive publication and reuse."""

import json
from collections import Counter

import pytest

from openkb.agent.evidence_verifier import verify_content
from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from openkb.processing import ProcessingIncomplete
from tests.http_model_fixture import evidence_response
from tests.test_generation_scopes import payload


@pytest.mark.parametrize("kind", ["coverage", "uncertainty", "presentation"])
def test_advisory_publishes_without_repair_and_preserves_coverage_on_reuse(
    kb_dir, tmp_path, model_service, kind
):
    source = tmp_path / "review.md"
    source.write_text("The listener uses port 9342. Its display label is Metrics.")
    calls = Counter()

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        calls[request["stage"]] += 1
        if request["stage"] == "verification":
            return {
                "verdict": "advisory" if kind == "uncertainty" else "supported",
                "reason": "No material error; retain the noncritical notice.",
                "issues": [],
                "advisories": [
                    {
                        "kind": kind,
                        "candidate": "",
                        "occurrences": ["e1"],
                        "reason": "Nonessential descriptive detail may need attention.",
                    }
                ],
            }
        result = evidence_response(request)
        if request["stage"] == "generation":
            result["content"] = "The listener uses port 9342."
        return result

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert not result.omissions
    assert list((kb_dir / "wiki/concepts").glob("*.md"))
    assert calls["generation"] == calls["verification"] == 1
    assert "knowledge_review_" + kind in result.warnings
    assert result.coverage["status"] == ("complete" if kind == "presentation" else "partial")
    if kind != "presentation":
        assert all(row["status"] == "pending" for row in result.coverage["ranges"])
        assert result.coverage["issues"][0]["reason"] == "knowledge_review_" + kind
    summary = next((kb_dir / "wiki/summaries").glob("*.md"))
    assert "内容复核提示" in summary.read_text()
    from openkb.application.source_artifacts import compilation_artifacts

    previews = compilation_artifacts(
        kb_dir, result.source_id, result.input_version, result.parse_id, "generation"
    )["records"]
    assert previews
    assert all("尚未通过校验" not in record["text"] for record in previews)
    if kind == "uncertainty":
        assert all("存在待复核内容" in record["text"] for record in previews)
    before = calls.copy()
    duplicate = import_document(kb_dir, source)
    assert duplicate.status == "skipped"
    assert duplicate.warnings == result.warnings
    assert duplicate.coverage == result.coverage
    resumed = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert resumed.coverage == result.coverage
    assert "knowledge_review_" + kind in resumed.warnings
    assert "内容复核提示" in summary.read_text()
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


@pytest.mark.parametrize("malformed_neighbor", [False, True])
def test_related_candidates_share_one_review_with_independent_decisions(
    kb_dir, tmp_path, model_service, malformed_neighbor
):
    import threading

    import yaml

    config = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config.read_text())
    settings["processing"].update(concurrency=2, context_tokens=16384, output_tokens=2048)
    config.write_text(yaml.safe_dump(settings))
    source = tmp_path / "related.md"
    source.write_text("# Listener\n\nAlpha uses port 9342. Beta uses port 9343.")
    arrived = threading.Barrier(2)
    batches = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        value = evidence_response(request)
        if request["stage"] == "facts":
            for unit, row in zip(request["units"], value["units"], strict=True):
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Heading")
                else:
                    row["facts"] = [
                        {"topic": label, "statement": quote, "quote": quote}
                        for label, quote in [
                            ("Alpha", "Alpha uses port 9342."),
                            ("Beta", "Beta uses port 9343."),
                        ]
                    ]
        elif request["stage"] == "planning":
            value = {
                "topics": [
                    {"name": title.lower(), "title": title, "kind": "concept", "members": [uid]}
                    for uid, title in request["topic_labels"].items()
                ]
            }
        elif request["stage"] == "generation":
            arrived.wait(timeout=5)
        elif request["stage"] == "verification_batch":
            batches.append(request)
            value = {
                "reviews": [
                    {
                        "id": candidate["id"],
                        "review": {
                            "verdict": "supported"
                            if candidate["title"] == "Alpha"
                            else "uncertain",
                            "reason": "Supported original."
                            if candidate["title"] == "Alpha"
                            else "Required scope is unresolved.",
                        },
                    }
                    for candidate in request["candidates"]
                ]
            }
            if malformed_neighbor:
                for candidate, review in zip(request["candidates"], value["reviews"], strict=True):
                    if candidate["title"] == "Alpha":
                        review["review"] = {}
        elif request["stage"] == "dependencies":
            value = {
                "topics": [
                    {
                        "path": row["path"],
                        "status": "independent",
                        "reason": "Alpha port is independent of Beta.",
                    }
                    for row in request["candidates"]
                ]
            }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(batches) == 1 and len(batches[0]["candidates"]) == 2
    assert (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert any(row["reason"] == "knowledge_evidence_mismatch" for row in result.omissions)
    individual_reviews = [
        json.loads(row["messages"][-1]["content"])
        for row in model_service
        if json.loads(row["messages"][-1]["content"])["stage"] == "verification"
    ]
    assert [row["title"] for row in individual_reviews] == (["Alpha"] if malformed_neighbor else [])

    before = len(model_service)
    settings["processing"]["concurrency"] = 1
    config.write_text(yaml.safe_dump(settings))
    resumed = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert not any(
        json.loads(row["messages"][-1]["content"])["stage"].startswith("verification")
        for row in model_service[before:]
    )
