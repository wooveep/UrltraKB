"""Budget projections preserve the whole directory and explicit suggestion status."""

import json

from openkb.agent.document_global_context import fits, freeze_context, messages_for
from openkb.processing import RequestLimits
from tests.test_document_markdown_planning import SETTINGS
from tests.test_document_orchestrator import _DummyParsed, _DummySource


def context_fixture(count=40):
    source, parsed = _DummySource(), _DummyParsed(count)
    settings = {
        **SETTINGS,
        "model": "gpt-4o",
        "processing": {
            **SETTINGS["processing"],
            "context_tokens": 4200,
            "max_context_tokens": 4200,
            "output_tokens": 700,
            "max_output_tokens": 700,
        },
    }
    limits = RequestLimits.from_config(settings)
    state = {
        "pages": [],
        "tasks": {},
        "windows": [],
        "overview_snapshot": None,
        "retained_fragments": [],
        "fragments": {},
        "deferred_suggestions": [],
    }
    nodes = [
        {
            "id": str(i),
            "title": f"Chapter {i}: " + "Detailed procedure context " * 12,
            "summary": "",
            "parent": None,
            "start": i,
            "end": i + 1,
        }
        for i in range(count)
    ]
    snapshot = freeze_context(
        state, {"nodes": nodes}, source, parsed, [], settings, limits, [], "", []
    )
    return snapshot, state, source, parsed, settings, limits


def test_large_flat_directory_can_admit_a_real_topic_group():
    snapshot, state, source, parsed, settings, limits = context_fixture()
    context = json.loads(snapshot["context_json"])
    assert context["projection"]["total_nodes"] == 40
    assert len(context["topics"]) < 40
    assert context["projection"]["coarsened_nodes"] == 40
    assert context["topics"][0]["from_section_key"] == "section:0"
    assert context["topics"][-1]["through_section_key"] == "section:39"
    assert fits(
        messages_for(
            snapshot, snapshot["nodes"][:1], state, source, parsed, settings, limits, [], "", []
        ),
        settings,
        limits,
    )


def test_deferred_details_and_omissions_are_projected_without_promoting_them():
    snapshot, state, source, parsed, settings, limits = context_fixture(2)
    state["deferred_suggestions"] = [
        {
            "title": f"Author {i}",
            "kind": "entity",
            "reason": "conditional_recommendation",
            "purpose": "Only create a page if tracking authors. " * 80,
            "notes": ["Author tracking is not currently required. " * 80],
        }
        for i in range(20)
    ]
    messages = messages_for(
        snapshot,
        snapshot["nodes"][:1],
        state,
        source,
        parsed,
        settings,
        limits,
        [],
        "",
        [],
        detail=False,
        shown=1,
    )
    carry = json.loads(messages[-1]["content"])["carry"]
    assert carry["pages"] == []
    assert len(carry["deferred_suggestions"]) == 1
    assert carry["deferred_suggestions"][0]["reason"] == "conditional_recommendation"
    assert carry["suggestions"]["omitted"]
    assert fits(messages, settings, limits)
    assert len(state["deferred_suggestions"][0]["notes"][0]) > 1000
