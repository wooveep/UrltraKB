"""Published summary metadata and labels stay valid for ordinary technical titles."""

import json

from openkb.application.documents import import_document
from openkb.lint import find_missing_okf_fields, find_orphans
from tests.http_model_fixture import evidence_response


def test_summary_preserves_bracketed_title_without_breaking_navigation(kb_dir, model_service):
    title = "配置 [service] | [db]"

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "planning":
            value["topics"][0]["title"] = title
        if payload["stage"] == "generation":
            value["content"] = "# " + title + "\nConfirmed knowledge."
        return value

    model_service.respond = respond
    original = kb_dir / "manual.md"
    original.write_text("Required service configuration.")
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert find_missing_okf_fields(kb_dir / "wiki") == []
    assert find_orphans(kb_dir / "wiki") == []
    summary = next((kb_dir / "wiki/summaries").glob("*.md")).read_text()
    assert "&#91;service&#93;" in summary and "&#124;" in summary
