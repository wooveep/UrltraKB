"""Model payloads keep original meaning without serializing archival duplication."""

import json

from openkb.agent.evidence_payload import project_evidence


def worksheet():
    text = "formula result\n\nhidden data"
    cell = {
        "schema_version": 1,
        "coordinate": "C4",
        "row": 4,
        "column": 3,
        "value": "=SUM(A1:A3)",
        "display": "formula result",
        "data_type": "f",
        "formula": "=SUM(A1:A3)",
        "cached": None,
        "cache_status": "missing",
        "formula_status": "available",
        "hidden_row": True,
        "hidden_column": False,
    }
    origin = {
        "normalized_span": [0, 14],
        "original_span": [0, 0],
        "kind": "sheet_cell",
        "sheet_cell": {"sheet_key": "s1", "sheet_name": "Sheet", "cell": cell},
    }
    return {
        "content": text,
        "source_spans": [[0, len(text)]],
        "unit_kind": "block",
        "sheet": {
            "key": "s1",
            "name": "Sheet",
            "state": "hidden",
            "merged_ranges": ["C4:D4"],
            "cells": [],
        },
        "origin_locators": [origin],
        "units": [
            {
                "ordinal": 1,
                "content": text,
                "origin_locators": [origin],
                "source_spans": [[0, len(text)]],
            }
        ],
        "block_range": [1],
        "coverage": "complete",
    }


def test_projection_preserves_formula_merge_and_hidden_semantics_without_duplicate_text():
    source = worksheet()
    before = json.dumps(source)
    result = project_evidence(source)
    cell = result["cells"][0]
    assert cell["coordinate"] == "C4" and cell["cache_status"] == "missing"
    assert cell["formula"] == "=SUM(A1:A3)" and cell["hidden_row"] is True
    assert result["sheet"]["merged_ranges"] == ["C4:D4"]
    assert result["content"] == source["content"]
    assert "schema_version" not in json.dumps(result) and "display" not in cell
    assert json.dumps(source) == before


def test_partial_cell_never_recovers_full_value_or_formula():
    source = worksheet()
    location = source["origin_locators"][0]["sheet_cell"]
    location["value_complete"] = False
    for field in ("value", "display", "formula", "cached"):
        location["cell"].pop(field)
    cell = project_evidence(source)["cells"][0]
    assert cell["value_complete"] is False and "formula" not in cell


def test_serialized_budget_is_enforced_with_explicit_continuation():
    content = "\n\n".join(f"paragraph {i} " + "x" * 100 for i in range(30))
    selected = {"content": content, "source_spans": [[0, len(content)]], "unit_kind": "text"}
    parts = []
    offset = 0
    while True:
        page = project_evidence(selected, offset=offset, budget=1500)
        assert len(json.dumps(page, ensure_ascii=False)) <= 1500
        parts.append(page["content"])
        if page["continuation"] is None:
            break
        assert page["coverage"] == "partial"
        offset = page["continuation"]
    assert "".join(parts) == content
