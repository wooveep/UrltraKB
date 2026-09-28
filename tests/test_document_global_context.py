"""Budget projections preserve the whole directory and explicit suggestion status."""

import json

import pytest

from openkb.agent.document_global_context import fits, freeze_context, messages_for
from openkb.agent.source_protocol import request_payload
from openkb.processing import RequestLimits
from tests.test_document_markdown_planning import SETTINGS
from tests.test_document_orchestrator import _DummyParsed, _DummySource


def test_common_inputs_precede_tasks_and_remain_exact_with_supplemental_evidence():
    from openkb.agent.document_global_context import empty_evidence
    from openkb.agent.document_protocol import plan_messages
    from openkb.processing import ProcessingIncomplete

    snapshot, _, source, parsed, _, _ = context_fixture(2)
    context = json.loads(snapshot["context_json"])
    evidence = empty_evidence(source, parsed)
    inputs, prefixes = [], []
    for subtask, recovery in (("overview", ""), ("pages", ""), ("pages", "Repair location")):
        if recovery:
            evidence = {**evidence, "blocks": [{"id": "d" * 64, "order": 0, "text": "Original"}]}
        messages = plan_messages(
            evidence,
            {"progress": subtask},
            {},
            [],
            "",
            [],
            "",
            subtask=subtask,
            recovery=recovery,
            planning_context=context,
        )
        inputs.append("\n".join(message["content"] for message in messages))
        prefixes.append(messages[1]["content"])
    assert prefixes[0] == prefixes[1] == prefixes[2]
    assert all(
        value.count('"schema":') == 1 and value.count('"source_conditions":') == 1
        for value in inputs
    )
    assert '"supplemental_evidence":' not in prefixes[-1]
    assert '"supplemental_evidence":' in inputs[-1]
    with pytest.raises(ProcessingIncomplete, match="planning_recovery_identity_mismatch"):
        plan_messages(evidence, {}, {}, [], "", [], "Changed rules", planning_context=context)


def test_runtime_values_are_single_structured_inputs_not_template_substitutions():
    from openkb.agent.document_global_context import empty_evidence
    from openkb.agent.document_protocol import plan_messages

    snapshot, _, source, parsed, _, _ = context_fixture(2)
    context = json.loads(snapshot["context_json"])
    context["common_inputs"]["language"] = "zh-CN"
    context["common_inputs"]["entity_types"] = ["product"]
    literal = 'code = {"language": "{language}", "types": "__ENTITY_TYPES__"}'
    context["topics"][0]["summary"] = literal
    carry = {"overview": {"text": "Saved overview {overview}", "partial": True}}
    messages = plan_messages(
        empty_evidence(source, parsed),
        carry,
        {"kind": "global_pages"},
        [],
        "",
        ["product"],
        "",
        "zh-CN",
        planning_context=context,
    )
    content = "\n".join(message["content"] for message in messages)
    payload = request_payload(messages)
    assert payload["planning_context"]["topics"][0]["summary"] == literal
    assert payload["carry"] == carry
    assert content.count("Saved overview {overview}") == 1
    assert content.count('"entity_types":') == 1
    assert "language" not in payload and "entity_types" not in payload
    assert payload["planning_context"]["common_inputs"]["language"] == "zh-CN"
    assert "{language}" not in payload["task_rules"]
    assert "__ENTITY_TYPES__" not in payload["task_rules"]


@pytest.mark.parametrize("change", ["schema", "catalog", "source"])
def test_freeze_refuses_changed_dependencies_without_overwriting_saved_context(change):
    from copy import deepcopy

    from openkb.processing import ProcessingIncomplete

    snapshot, state, source, parsed, settings, limits = context_fixture(2)
    old = deepcopy(snapshot)
    nodes = [
        {
            "id": str(i),
            "title": f"Chapter {i}: " + "Detailed procedure context " * 12,
            "summary": "",
            "parent": None,
            "start": i,
            "end": i + 1,
        }
        for i in range(2)
    ]
    if change == "source":
        source = deepcopy(source)
        source.id = "e" * 64
    with pytest.raises(ProcessingIncomplete, match="planning_recovery_identity_mismatch"):
        freeze_context(
            state,
            {"nodes": nodes},
            source,
            parsed,
            [("concepts/new", "New", "Description")] if change == "catalog" else [],
            settings,
            limits,
            [],
            "New rules" if change == "schema" else "",
            [],
        )
    assert state["planning_snapshot"] == old


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
