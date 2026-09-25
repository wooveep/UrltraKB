"""Concise contributions retain safe source references without hiding required facts."""


import pytest

from openkb.agent.evidence_generation_protocol import normalize_output
from openkb.agent.evidence_retry import ResponseIncomplete
from tests.test_generation_scopes import payload, response


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
