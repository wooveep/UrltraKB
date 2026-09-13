"""Model navigation cannot silently publish a reversed field relationship."""

import json

import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_history import source_status
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


def test_rejected_navigation_summary_retains_original_navigation(kb_dir, tmp_path, model_service):
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"].update(context_tokens=32768, output_tokens=4096, max_requests=30)
    config["navigation"] = {"enabled": True}
    path.write_text(yaml.safe_dump(config))
    source = tmp_path / "fields.md"
    source.write_text(
        "# Fields\n\n" + "The left field is the minimum; the right field is the maximum. " * 6
    )
    checked = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        result = evidence_response(payload)
        if payload["stage"] == "index_summary":
            for row in result["summaries"]:
                row["summary"] = "The left field is the maximum."
        elif payload["stage"] == "index_summary_verification":
            checked.append(payload)
            return {
                "summaries": [
                    {
                        "id": row["id"],
                        "verdict": "unsupported",
                        "reason": "Minimum and maximum are reversed.",
                    }
                    for row in payload["candidates"]
                ]
            }
        return result

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert checked
    navigation = source_status(kb_dir, result.source_id)["navigation"]
    assert navigation["status"] == "degraded"
    assert all(node["summary"] != "The left field is the maximum." for node in navigation["nodes"])
    assert any("minimum" in node["summary"] for node in navigation["nodes"])


@pytest.mark.parametrize(
    "broken_stage", ["index_structure", "index_summary", "index_summary_verification"]
)
def test_duplicate_navigation_fields_keep_originals_and_continue(
    kb_dir, tmp_path, model_service, monkeypatch, broken_stage
):
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["navigation"] = {"enabled": True}
    path.write_text(yaml.safe_dump(config))
    source = tmp_path / "original.md"
    source.write_text("The lower field is the minimum and the upper field is the maximum. " * 7)
    broken = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = response(evidence_response(payload))
        if payload["stage"] == broken_stage:
            broken.append(payload)
            value.choices[0].message.content = '{"summaries":[],"summaries":[]}'
        return value

    monkeypatch.setattr(litellm, "completion", completion)
    result = import_document(kb_dir, source)
    assert broken
    assert result.knowledge_compilation == "completed", result
    assert source_status(kb_dir, result.source_id)["navigation"]["status"] == "degraded"
