"""Continue retains settled topic identities while repairing only missing content."""

import json

import litellm
import pytest

from openkb.application.documents import import_document
from openkb.application.pages import read_page, save_page
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize(
    "manual_edit,gap_stage,target_change,link_form",
    [
        (False, "planning", None, "wiki"),
        (True, "planning", None, "wiki"),
        (False, "generation", None, "wiki"),
        (True, "generation", None, "wiki"),
        (False, "generation", "add", "wiki"),
        (False, "generation", "remove", "wiki"),
        (False, "generation", "remove", "markdown"),
        (False, "generation", "remove", "reference"),
        (False, "generation", "remove", "html"),
        (False, "generation", "remove", "literal"),
    ],
)
def test_continue_keeps_published_topic_plan_and_only_generates_the_failed_topic(
    kb_dir, tmp_path, monkeypatch, manual_edit, gap_stage, target_change, link_form
):
    from openkb.locks import atomic_write_text

    phase, calls, planning_members = 1, [], []
    reference = kb_dir / "wiki/concepts/reference.md"
    if target_change == "remove":
        atomic_write_text(reference, "# An independently maintained reference page\n")
    links = {
        "wiki": "[[concepts/reference]]",
        "markdown": "[Reference](reference.md)",
        "reference": "[Reference][ref]\n\n[ref]: ../concepts/%72eference.md#heading",
        "html": '<a href="reference.md">Reference</a>',
        "literal": "`[Example](reference.md)`\n\n```md\n[Example](reference.md)\n```\n"
        "[External](https://example.com/reference.md)",
    }

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        calls.append((phase, stage, payload.get("title")))
        value = evidence_response(payload)
        if stage == "facts":
            for row, unit in zip(value["units"], payload["units"]):
                row["facts"][0]["topic"] = unit["text"].split()[0]
        elif stage == "planning":
            planning_members.append((phase, list(payload["topic_labels"].values())))
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
                    if not (phase == 1 and gap_stage == "planning" and label == "Beta")
                ]
            }
        elif (
            stage == "generation"
            and phase == 1
            and gap_stage == "generation"
            and payload["title"] == "Beta"
        ):
            value["covered"] = []
        if (
            stage == "generation"
            and phase == 1
            and payload["title"] == "Alpha"
            and target_change == "remove"
        ):
            for fragment in value.get("fragments", [value]):
                fragment["content"] += " " + links[link_form]
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    source = tmp_path / "small.md"
    source.write_text("Alpha requirement.\n\nBeta requirement.")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    assert any(row["stage"] == gap_stage for row in first.omissions)
    page = read_page(kb_dir, "concepts/alpha")
    if manual_edit:
        body = page.body.replace(
            "<!-- /openkb-source:", "Manual addition.\n<!-- /openkb-source:", 1
        )
        assert save_page(kb_dir, page.path, body, version=page.version).status == "saved"
        page = read_page(kb_dir, page.path)
    if target_change == "add":
        atomic_write_text(reference, "# A newly added independent page\n")
    elif target_change == "remove":
        reference.unlink()  # An external wiki file change, not a model/tool replacement.
    phase = 2
    result = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert result.knowledge_compilation == "completed", result
    assert not result.omissions
    assert not any(p == 2 and stage == "facts" for p, stage, _ in calls)
    assert all("Alpha" not in members for p, members in planning_members if p == 2)
    expected = ["Replanned Beta" if gap_stage == "planning" else "Beta"]
    used_link_removed = target_change == "remove" and link_form != "literal"
    if used_link_removed:
        expected.append("Alpha")
    assert sorted(title for p, stage, title in calls if p == 2 and stage == "generation") == sorted(
        expected
    )
    if used_link_removed:
        assert "reference.md" not in read_page(kb_dir, page.path).body
        assert "[[concepts/reference]]" not in read_page(kb_dir, page.path).body
    else:
        assert read_page(kb_dir, page.path).content == page.content
    assert read_page(kb_dir, "concepts/beta").body


def test_oversized_planning_topic_finishes_with_a_visible_omission(kb_dir, tmp_path, monkeypatch):
    from openkb.config import load_config, save_config

    config = load_config(kb_dir / ".openkb/config.yaml")
    config["processing"].update(context_tokens=4096, output_tokens=1024, max_output_tokens=1024)
    save_config(kb_dir / ".openkb/config.yaml", config)

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert payload["stage"] == "facts"
        value = evidence_response(payload)
        for unit in value["units"]:
            unit["facts"][0]["topic"] = "A very long proposed topic " * 2000
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    source = tmp_path / "small.md"
    source.write_text("A short source requirement.")
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert any(row["reason"] == "topic_context_exceeds_request_budget" for row in result.omissions)
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    assert list((kb_dir / "wiki/summaries").glob("*.md"))
