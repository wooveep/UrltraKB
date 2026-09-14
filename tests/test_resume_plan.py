"""Continue retains settled topic identities while repairing only missing content."""

import json

import litellm
import pytest

from openkb.application.documents import import_document
from openkb.application.pages import read_page, save_page
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize("manual_edit", [False, True])
def test_continue_keeps_published_topic_plan_and_only_generates_the_failed_topic(
    kb_dir, tmp_path, monkeypatch, manual_edit
):
    phase, calls = 1, []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        calls.append((phase, stage, payload.get("title")))
        value = evidence_response(payload)
        if stage == "facts":
            for row, unit in zip(value["units"], payload["units"]):
                row["facts"][0]["topic"] = unit["text"].split()[0]
        elif stage == "planning":
            # A real planner can rename existing topics after seeing its own
            # published catalogue. Continue should not ask it to plan them again.
            value = {
                "topics": [
                    {
                        "name": label.lower(),
                        "title": label if phase == 1 else "Replanned " + label,
                        "kind": "concept",
                        "members": [identity],
                    }
                    for identity, label in payload["topic_labels"].items()
                ]
            }
        elif stage == "generation" and phase == 1 and payload["title"] == "Beta":
            value["covered"] = []
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    source = tmp_path / "small.md"
    source.write_text("Alpha requirement.\n\nBeta requirement.")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    assert any(row["reason"] == "topic_generation_incomplete" for row in first.omissions)
    page = read_page(kb_dir, "concepts/alpha")
    if manual_edit:
        body = page.body.replace(
            "<!-- /openkb-source:", "Manual addition.\n<!-- /openkb-source:", 1
        )
        assert save_page(kb_dir, page.path, body, version=page.version).status == "saved"
        page = read_page(kb_dir, page.path)
    phase = 2
    result = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert result.knowledge_compilation == "completed", result
    assert not result.omissions
    assert not any(p == 2 and stage in {"facts", "planning"} for p, stage, _ in calls)
    assert [title for p, stage, title in calls if p == 2 and stage == "generation"] == ["Beta"]
    assert read_page(kb_dir, page.path).content == page.content
    assert read_page(kb_dir, "concepts/beta").body
