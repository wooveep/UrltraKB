"""Pending originals are resolved from real partial publication, without rerunning models."""

from copy import deepcopy

import pytest

from openkb.application.documents import import_document
from openkb.application.source_actions import read_source_evidence
from openkb.application.source_issues import source_issues
from openkb.evidence import Evidence
from tests.test_compilation_omissions import setup as omission_fixture

setup = omission_fixture


def inspect(kb, result, **kwargs):
    return source_issues(
        kb,
        result.source_id,
        result.input_version,
        result.parse_id,
        result.coverage,
        result.omissions,
        **kwargs,
    )


def test_excluded_content_opens_original_and_keeps_verified_sibling_out(kb_dir, setup):
    source, state, calls = setup
    state["stage"] = "verification"
    result = import_document(kb_dir, source)
    before = calls.copy()
    view = inspect(kb_dir, result)
    assert view["excluded"] == 1 and view["pending"] == 1
    assert view["rows"][0]["reference"]
    original = read_source_evidence(
        kb_dir, Evidence(**view["rows"][0]["reference"]), max_chars=1000
    )
    assert original.text == "Beta requirement."
    assert view["rows"][0]["reason"] == result.omissions[0]["reason"]
    assert view["rows"][0]["item"] == result.omissions[0]["items"][0]
    assert calls == before


def test_invalid_source_and_coverage_are_rejected(kb_dir, setup):
    source, _, _ = setup
    result = import_document(kb_dir, source)
    with pytest.raises(ValueError, match="identit"):
        source_issues(
            kb_dir,
            "b" * 32,
            result.input_version,
            result.parse_id,
            result.coverage,
            result.omissions,
        )
    coverage = deepcopy(result.coverage)
    coverage["ranges"][0]["end"] += 1
    with pytest.raises(ValueError, match="denominator"):
        source_issues(
            kb_dir,
            result.source_id,
            result.input_version,
            result.parse_id,
            coverage,
            result.omissions,
        )


def test_old_history_does_not_invent_original_location_and_paginates(kb_dir, setup):
    source, _, calls = setup
    result = import_document(kb_dir, source)
    before = calls.copy()
    omissions = [
        {
            "stage": "generation",
            "reason": "knowledge_evidence_mismatch",
            "items": ["concepts/old-" + str(i) for i in range(53)],
        }
    ]
    args = (kb_dir, result.source_id, result.input_version, result.parse_id, {}, omissions)
    first = source_issues(*args)
    second = source_issues(*args, offset=first["next_offset"])
    assert first["total"] == 53 and first["excluded"] == 53
    assert not first["coverage_known"] and first["pending"] == 0
    assert len(first["rows"]) == 50 and len(second["rows"]) == 3
    assert all(row["reference"] is None for row in first["rows"])
    assert not ({row["item"] for row in first["rows"]} & {row["item"] for row in second["rows"]})
    assert calls == before


def test_complete_coverage_has_no_issue_rows(kb_dir, setup):
    source, state, _ = setup
    state["broken"] = False
    result = import_document(kb_dir, source)
    assert inspect(kb_dir, result)["total"] == 0


def test_current_issue_index_does_not_hydrate_unselected_request_bodies(kb_dir, setup, monkeypatch):
    from openkb.application import source_artifacts

    source, _, _ = setup
    result = import_document(kb_dir, source)
    read = source_artifacts.read_object

    def indexed_only(path):
        if path.parent.name != "latest":
            raise AssertionError(f"Unexpected artifact hydration: {path.name}")
        return read(path)

    monkeypatch.setattr(source_artifacts, "read_object", indexed_only)

    view = inspect(kb_dir, result)

    assert view["rows"][0]["item"] == "concepts/beta"
