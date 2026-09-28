"""Runtime facts are independent of projections and model output wording."""

import json
from copy import deepcopy

import pytest

from openkb.agent.document_planning_runtime import (
    build_planning_runtime,
    preparation_max_chars,
    read_catalog,
    registered_sources,
    selection_counts,
)
from openkb.agent.source_protocol import request_payload
from openkb.processing import RequestLimits
from openkb.state import HashRegistry
from tests.test_document_global_planning import run_global
from tests.test_document_markdown_planning import SETTINGS
from tests.test_document_orchestrator import _DummyParsed, _DummySource


@pytest.mark.parametrize(
    "count,targets,read,expected",
    [
        (0, set(), 0, "initial"),
        (2, set(), 0, "initial"),
        (3, set(), 0, "established"),
        (0, {"concepts/manual"}, 1, "established"),
        (None, set(), 0, "unknown"),
        (1, {"concepts/unread"}, 0, "unknown"),
    ],
)
def test_stage_uses_registered_facts_not_displayed_directory(count, targets, read, expected):
    facts = {
        "targets": targets,
        "read": read,
        "registered_other_sources": count,
        "registry_status": "available",
        "registry_binding": "binding",
    }
    runtime = build_planning_runtime(
        _DummySource(), _DummyParsed(1), facts, RequestLimits.from_config(SETTINGS)
    )
    assert runtime["kb_stage"] == expected
    assert runtime["catalog_status"]["total"] == len(targets)


def test_catalog_restores_briefs_and_does_not_invent_source_counts(tmp_path):
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities/tool.md").write_text(
        "---\ntitle: Tool\ndescription: Purpose\nbrief: Old\ntype: product\n"
        "sources: [one, one, two]\n---\n# Wrong heading\nBody"
    )
    (tmp_path / "entities/other.md").write_text(
        "---\nbrief: Legacy description\nsources: bad-shape\n---\n# Other\nBody"
    )
    entries, metadata = read_catalog(
        tmp_path, {"entities/tool", "entities/other", "entities/missing"}, ["product"]
    )
    assert ("entities/tool", "Tool", "Purpose") in entries
    assert ("entities/other", "Other", "Legacy description") in entries
    assert metadata["entities/tool"]["source_count"] == 2
    assert metadata["entities/other"]["source_count"] is None
    assert metadata["entities/tool"]["type"] == "product"


def test_registry_excludes_current_and_keeps_legacy_unknown(tmp_path):
    source = _DummySource()
    registry = HashRegistry(tmp_path / ".openkb/hashes.json")
    registry.add(source.source_id, {"name": "current"})
    registry.add("b" * 32, {"name": "other"})
    assert registered_sources(tmp_path, source)["registered_other_sources"] == 1
    registry.add("c" * 64, {"name": "unbound legacy"})
    assert registered_sources(tmp_path, source)["registered_other_sources"] is None


def test_preparation_character_policy_excludes_attachments_at_exact_boundary():
    limits = RequestLimits.from_config(SETTINGS)
    parsed = _DummyParsed(2)
    parsed.blocks[0].chars = preparation_max_chars(limits)
    parsed.blocks[1].location = {"attachment": "asset"}
    facts = {
        "targets": set(),
        "read": 0,
        "registered_other_sources": 0,
        "registry_status": "available",
        "registry_binding": "binding",
    }
    runtime = build_planning_runtime(_DummySource(), parsed, facts, limits)
    assert runtime["source_body_chars"] == preparation_max_chars(limits)
    assert not runtime["whole_source_exceeds_preparation"]
    parsed.blocks[0].chars += 1
    assert build_planning_runtime(_DummySource(), parsed, facts, limits)[
        "whole_source_exceeds_preparation"
    ]


def test_counts_do_not_charge_updates_or_repeated_new_pages():
    pages = [{"key": str(i), "kind": "concept", "name": f"concepts/new-{i}"} for i in range(3)]
    pages += [deepcopy(pages[0]), {"key": "update", "kind": "concept", "target": "concepts/old"}]
    assert selection_counts(pages, {"concepts/old"})["concept"] == {"create": 3, "update": 1}


def test_real_builder_binds_runtime_without_extra_calls_and_preserves_prefix(tmp_path):
    requests = []

    def respond(messages, **kwargs):
        requests.append(messages)
        return (
            "Useful overview."
            if request_payload(messages)["subtask"] == "overview"
            else "## Create concepts\n- Calibration\n  Subject: Calibration"
        )

    result = run_global(tmp_path, respond)
    assert len(requests) == 2 and requests[0][:2] == requests[1][:2]
    payload = request_payload(requests[1])
    runtime = payload["planning_context"]["runtime"]
    assert runtime["kb_stage"] == "initial"
    assert "concept_selection_guidance" in payload["task_rules"]
    assert "scope_guidance" in payload["task_rules"]
    assert payload["carry"]["selection_counts"]["concept"]["create"] == 0
    assert result.plan.metadata["planning_runtime"] == runtime
    assert (
        json.loads(requests[0][1]["content"])["planning_context"]
        == json.loads(requests[1][1]["content"])["planning_context"]
    )
