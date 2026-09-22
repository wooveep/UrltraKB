"""Concise contributions retain safe source references without hiding required facts."""

import json
from collections import Counter

import pytest

from openkb.agent.evidence_generation_protocol import normalize_output
from openkb.agent.evidence_retry import ResponseIncomplete
from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.test_generation_scopes import payload, response


@pytest.mark.parametrize("advisory", [False, True])
def test_source_only_detail_is_explicit_and_reused_without_new_model_calls(
    kb_dir, tmp_path, model_service, advisory
):
    source = tmp_path / "brief.md"
    core = "Use port 9342 only on version 2."
    detail = "The UI labels this example Blue."
    source.write_text(core + "\n\n" + detail)
    calls = Counter()

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        calls[request["stage"]] += 1
        if request["stage"] == "planning":
            blocks = request["evidence"]["blocks"]
            target = request["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            return {
                "overview": {"text": "Port usage overview.", "ranges": ranges, "limitations": []},
                "page_changes": [
                    {
                        "local_key": "port",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/port-usage",
                        "title": "Port Usage",
                        "purpose": "Port configuration requirement",
                        "subject_ranges": [[blocks[0]["order"], blocks[0]["order"] + 1]],
                        "necessary_context": [],
                    }
                ],
                "source_only": [
                    {
                        "ranges": [[blocks[1]["order"], blocks[1]["order"] + 1]],
                        "reason": "UI label is source-only example metadata.",
                    }
                ],
                "unresolved": [],
                "resolutions": [],
            }
        if request["stage"] == "generation":
            assert detail not in "\n".join(row["text"] for row in request["evidence"]["blocks"])
            return {
                "content": core,
                "covered": [row["id"] for row in request["occurrences"]],
            }
        assert request["stage"] == "verification"
        assert detail not in "\n".join(row["text"] for row in request["evidence"]["blocks"])
        assert detail not in request["candidate"]["content"]
        return {
            "verdict": "advisory" if advisory else "supported",
            "reason": "The retained port guidance matches the supplied evidence.",
        }

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert not result.omissions
    assert not result.warnings
    assert result.coverage["status"] == "complete"
    assert [row["status"] for row in result.coverage["ranges"]] == [
        "verified",
        "no_facts",
    ]
    assert result.coverage["ranges"][1]["reason"] == "UI label is source-only example metadata."
    page = next((kb_dir / "wiki/concepts").glob("*.md"))
    text = page.read_text()
    assert core in text and detail not in text
    assert calls["generation"] == calls["verification"] == 1
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
        if stage == "planning":
            blocks = request["evidence"]["blocks"]
            target = request["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            return {
                "overview": {
                    "text": "Port requirement overview.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "port",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/port-configuration",
                        "title": "Port Configuration",
                        "purpose": "Restricted port configuration",
                        "subject_ranges": [[blocks[0]["order"], blocks[0]["order"] + 1]],
                        "necessary_context": [
                            {
                                "relation": "applicable_condition",
                                "ranges": [[blocks[1]["order"], blocks[1]["order"] + 1]],
                                "basis": blocks[1]["text"],
                                "basis_ranges": [[blocks[1]["order"], blocks[1]["order"] + 1]],
                            }
                        ],
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        if stage == "generation":
            if "revision" not in request:
                content = "Use port 9342."
            else:
                content = "Use port 9342 only on version 2."
            return {
                "content": content,
                "covered": [row["id"] for row in request["occurrences"]],
            }
        if stage == "verification" and "only on version 2" not in request["candidate"]["content"]:
            assert "This is available only on version 2." in "\n".join(
                row["text"] for row in request["evidence"]["blocks"]
            )
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
        return {"verdict": "supported", "reason": "The corrected condition is retained."}

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert stages["generation"] == stages["verification"] == 2
    assert [row["status"] for row in result.coverage["ranges"]] == ["verified", "referenced"]
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
