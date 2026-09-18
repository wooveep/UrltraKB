"""Concise contributions retain safe source references without hiding required facts."""

import json
import re
from collections import Counter
from urllib.parse import unquote

import pytest

from openkb.agent.evidence_generation_protocol import normalize_output
from openkb.agent.evidence_retry import ResponseIncomplete
from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response
from tests.test_generation_scopes import payload, response


@pytest.mark.parametrize("pending_review", [False, True])
def test_secondary_detail_stays_linked_and_is_reused_without_new_model_calls(
    kb_dir, tmp_path, model_service, pending_review
):
    source = tmp_path / "brief.md"
    core = "Use port 9342 only on version 2."
    detail = "The UI labels this example Blue."
    source.write_text(core + "\n\n" + detail)
    calls = Counter()

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        calls[request["stage"]] += 1
        result = evidence_response(request)
        if request["stage"] == "generation":
            assert "source_details" in request["output_contract"]
            result.update(content=core, source_details=["e2"])
        elif request["stage"] == "verification":
            assert request["source_details"] == ["e2"]
            assert detail in request["evidence"][1]["text"]
            assert detail not in request["content"]
            if pending_review:
                result.update(
                    verdict="advisory",
                    advisories=[
                        {
                            "kind": "uncertainty",
                            "candidate": "",
                            "occurrences": ["e2"],
                            "reason": "The optional label may require a later source review.",
                        }
                    ],
                )
        return result

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert not result.omissions
    assert bool(result.warnings) == pending_review
    assert result.coverage["status"] == ("partial" if pending_review else "complete")
    assert [row["status"] for row in result.coverage["ranges"]] == [
        "verified",
        "pending" if pending_review else "referenced",
    ]
    assert result.coverage["ranges"][1]["reason"] == (
        "knowledge_review_pending" if pending_review else "secondary_details_in_source"
    )
    page = next((kb_dir / "wiki/concepts").glob("*.md"))
    text = page.read_text()
    assert core in text and detail not in text
    link = re.search(r"\]\((\.\./sources/[^)]+\.md)\)", text)
    assert link
    assert detail in (page.parent / unquote(link[1])).read_text()
    assert calls["generation"] == calls["verification"] == 1
    from openkb.application.source_artifacts import compilation_artifacts

    previews = compilation_artifacts(
        kb_dir, result.source_id, result.input_version, result.parse_id, "generation"
    )["records"]
    assert previews and all("次要细节保留在原文" in item["text"] for item in previews)
    before = calls.copy()
    duplicate = import_document(kb_dir, source)
    assert duplicate.status == "skipped"
    assert duplicate.coverage == result.coverage
    resumed = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert resumed.coverage == result.coverage
    assert page.read_text() == text
    assert calls == before


def test_required_condition_cannot_be_waived_by_source_detail_selection(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "required.md"
    source.write_text("Use port 9342.\n\nThis is available only on version 2.")
    stages = Counter()

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        stage = request["stage"]
        stages[stage] += 1
        result = evidence_response(request)
        if stage == "generation":
            if "revision" not in request:
                result.update(content="Use port 9342.", source_details=["e2"])
            else:
                result.update(content="Use port 9342 only on version 2.", source_details=[])
        elif stage == "verification" and request.get("source_details"):
            return {
                "verdict": "unsupported",
                "reason": "The version condition is required for the retained instruction.",
                "issues": [
                    {
                        "kind": "missing",
                        "candidate": "",
                        "occurrences": ["e2"],
                        "reason": "Restore the version 2 restriction in the instruction.",
                    }
                ],
            }
        return result

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert stages["generation"] == stages["verification"] == 2
    assert all(row["status"] == "verified" for row in result.coverage["ranges"])
    text = next((kb_dir / "wiki/concepts").glob("*.md")).read_text()
    assert "only on version 2" in text
    assert "Details in source" not in text


def test_secondary_scope_can_use_a_reference_without_an_empty_task_section():
    p = payload()
    out = response(p)
    out["source_details"] = out["fragments"].pop()["occurrences"]
    normalized = normalize_output(out, p)
    assert len(normalized["fragments"]) == 2
    assert "Configuration 2" not in normalized["content"]
    assert normalized == normalize_output(normalized, p)


@pytest.mark.parametrize("details", [["missing"], ["e4", "e4"], "e4", [None], ["e1"]])
def test_invalid_or_double_counted_detail_selection_is_rejected(details):
    p, out = payload(), response(payload())
    out["fragments"].pop()
    out["source_details"] = details
    with pytest.raises(ResponseIncomplete, match="topic_generation_incomplete"):
        normalize_output(out, p)


def test_native_table_cells_cannot_be_individually_deferred():
    p = payload()
    out = response(p)
    out["source_details"] = out["fragments"].pop()["occurrences"]
    p["table_objects"] = [{"cells": [{"fact_id": "displays"}]}]
    with pytest.raises(ResponseIncomplete):
        normalize_output(out, p)
