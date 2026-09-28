"""Planning keeps a shared document prefix and one completed overview turn."""

import json
from copy import deepcopy

from openkb.agent.document_global_context import empty_evidence
from openkb.agent.document_planning_support import accepted_request_reference
from openkb.agent.document_protocol import plan_messages
from tests.test_document_global_context import context_fixture


def test_overview_pages_and_recovery_share_context_without_repeating_overview():
    snapshot, _, source, parsed, _, _ = context_fixture(2)
    context = json.loads(snapshot["context_json"])
    evidence = empty_evidence(source, parsed)
    overview = 'Saved overview with literal {language} and "quotes".'
    carry = {"overview": {"text": overview, "partial": True, "input_clipped": False}}
    before = deepcopy(carry)
    overview_request = plan_messages(
        evidence, {}, {}, [], "", [], "", subtask="overview", planning_context=context
    )
    pages = plan_messages(evidence, carry, {}, [], "", [], "", planning_context=context)
    retry = plan_messages(
        {**evidence, "blocks": [{"id": "d" * 64, "order": 0, "text": "Extra evidence"}]},
        carry,
        {},
        [],
        "",
        [],
        "",
        recovery="Retry only this selection",
        planning_context=context,
    )
    assert [m["role"] for m in pages] == ["system", "user", "assistant", "user"]
    assert overview_request[:2] == pages[:2] == retry[:2]
    assert pages[2]["content"] == overview
    assert "text" not in json.loads(pages[-1]["content"])["carry"]["overview"]
    assert sum(m["content"].count(overview) for m in pages) == 1
    assert carry == before
    assert "Extra evidence" not in retry[1]["content"]
    assert "Extra evidence" in retry[-1]["content"]
    assert json.loads(retry[-1]["content"])["carry"]["overview"]["partial"]


def test_request_receipt_changes_when_an_earlier_message_changes():
    rows = [
        {"role": "system", "content": "Fixed rules"},
        {"role": "user", "content": '{"planning_context":{"summary":"first"}}'},
        {"role": "assistant", "content": "Saved overview"},
        {"role": "user", "content": '{"stage":"planning","subtask":"pages"}'},
    ]
    changed = deepcopy(rows)
    changed[1]["content"] = '{"planning_context":{"summary":"second"}}'
    assert accepted_request_reference(rows) != accepted_request_reference(changed)
    changed = deepcopy(rows)
    changed[2]["content"] = "Different overview"
    assert accepted_request_reference(rows) != accepted_request_reference(changed)
