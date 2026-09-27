"""Formal document-plan and page-response failures remain bounded and recoverable."""

import json

import litellm

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


def test_successful_planned_page_survives_later_generation_omission(kb_dir, tmp_path, monkeypatch):
    source = tmp_path / "partial.md"
    source.write_text("Good evidence.\n\nUnavailable evidence.")

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        if payload["stage"] == "planning":
            assert payload["evidence"]["blocks"] == []
            if payload["subtask"] == "overview":
                value = "Two independent requirements."
            else:
                value = {
                    "pages": [
                        {
                            "kind": "concept",
                            "title": "Good",
                            "subject_ranges": [[0, 1]],
                        },
                        {
                            "kind": "concept",
                            "title": "Unavailable",
                            "subject_ranges": [[1, 2]],
                        },
                    ]
                }
        elif payload["stage"] == "generation" and payload["page"]["title"] == "Unavailable":
            value = {"content": "", "covered": []}
        else:
            value = evidence_response(payload)
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed"
    assert any(
        row["stage"] == "generation" and row["reason"] == "document_generation_incomplete"
        for row in result.omissions
    )
    assert len(list((kb_dir / "wiki/concepts").glob("good*.md"))) == 1
    assert not list((kb_dir / "wiki/concepts").glob("unavailable*.md"))
