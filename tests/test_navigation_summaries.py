"""Summary transport uses local section numbers and durable validation diagnostics."""

import json

import litellm
import pytest

from openkb.config import resolve_credential_bundle
from openkb.locks import kb_ingest_lock
from openkb.navigation import prepare_navigation, read_navigation
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response as model_response
from tests.test_navigation_windows import prepare


def test_partial_summary_keeps_good_item_and_only_recovers_missing_node(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "partial-summary.md"
    path.write_text("# Install\n\nUse version 7.\n\n# Repair\n\nRestart once.")
    requested = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "index_structure":
            return {"sections": []}
        requested.append([row["id"] for row in payload["nodes"]])
        if len(requested) == 1:
            return {
                "summaries": [
                    {"id": "1", "summary": "Required version."},
                    {"id": "unknown", "summary": "Unbound summary."},
                ]
            }
        return {"summaries": [{"id": "2", "summary": "Recovery instructions."}]}

    model_service.respond = respond
    _, _, _, saved = prepare(kb_dir, path, {"summaries": True})
    assert requested == [["1", "2"], ["2"]]
    assert [n["summary"] for n in saved["nodes"][1:]] == [
        "Required version.",
        "Recovery instructions.",
    ]
    assert saved["status"] == "enhanced"


def test_summary_numbers_map_reordered_responses_to_persistent_nodes(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "sections.md"
    path.write_text("# Install\n\nUse version 7.\n\n# Repair\n\nRestart once.")
    requests = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        requests.append(payload)
        if payload["stage"] == "index_structure":
            return {"sections": []}
        return {
            "summaries": [
                {"id": "2", "summary": None},
                {"id": "1", "summary": "Required version."},
            ]
        }

    model_service.respond = respond
    source, _, _, saved = prepare(kb_dir, path, {"summaries": True})
    assert [p["stage"] for p in requests] == ["index_structure", "index_summary"]
    assert [row["id"] for row in requests[-1]["nodes"]] == ["1", "2"]
    assert saved["status"] == "enhanced", saved
    restored = read_navigation(kb_dir, source, identity=saved["id"], limit=200)
    install, repair = restored["nodes"][1:]
    assert install["title"] == "Install"
    assert (install["summary"], install["summary_origin"]) == ("Required version.", "model")
    assert repair["title"] == "Repair"
    assert (repair["summary"], repair["summary_origin"]) == ("", "unavailable")
    assert all(len(n["id"]) == 25 and n["parent"] == "n0" for n in (install, repair))


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        ([], "$: expected an object"),
        ({}, "$.summaries: missing required field"),
        ({"summaries": [], "extra": True}, '$["extra"]: unexpected field'),
        ({"summaries": {}}, "$.summaries: expected an array"),
        ({"summaries": [None]}, "$.summaries[0]: expected an object"),
        ({"summaries": [{"summary": None}]}, "$.summaries[0].id: missing required field"),
        ({"summaries": [{"id": "1"}]}, "$.summaries[0].summary: missing required field"),
        (
            {"summaries": [{"id": "1", "summary": None, "extra": True}]},
            '$.summaries[0]["extra"]: unexpected field',
        ),
        (
            {"summaries": [{"id": 1, "summary": None}]},
            "$.summaries[0].id: expected a string section number",
        ),
        (
            {"summaries": [{"id": "wrong", "summary": None}]},
            "$.summaries[0].id: unknown section number",
        ),
        (
            {"summaries": [{"id": "1", "summary": None}, {"id": "1", "summary": None}]},
            "$.summaries[1].id: duplicate section number 1",
        ),
        (
            {"summaries": [{"id": "1", "summary": None}]},
            "$.summaries: missing section numbers: 2",
        ),
        (
            {"summaries": [{"id": "1", "summary": False}]},
            "$.summaries[0].summary: expected a string or null",
        ),
        (
            {"summaries": [{"id": "1", "summary": ""}]},
            "$.summaries[0].summary: expected 1 to 1600 characters",
        ),
        (
            {"summaries": [{"id": "1", "summary": "x" * 1601}]},
            "$.summaries[0].summary: expected 1 to 1600 characters",
        ),
    ],
)
def test_rejected_summary_preserves_field_reason_on_reload_without_reasking(
    kb_dir, tmp_path, model_service, response, detail
):
    path = tmp_path / "sections.md"
    path.write_text("# Install\n\nUse version 7.\n\n# Repair\n\nRestart once.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        return {"sections": []} if payload["stage"] == "index_structure" else response

    model_service.respond = respond
    source, parsed, settings, saved = prepare(kb_dir, path, {"summaries": True})
    reason = "index_summary_invalid: " + detail
    assert saved["status"] == "degraded"
    assert saved["reason"] == reason
    assert saved["windows"][0]["reason"] == reason
    restored = read_navigation(kb_dir, source, identity=saved["id"], limit=200)
    assert restored["reason"] == reason
    assert all(n["summary_origin"] == "unavailable" for n in restored["nodes"])
    with kb_ingest_lock(kb_dir / ".openkb"):
        resumed = prepare_navigation(
            kb_dir, source, parsed, settings, bundle=resolve_credential_bundle(kb_dir)
        )
    assert resumed["id"] == saved["id"]
    assert resumed["reason"] == reason
    assert [json.loads(body["messages"][-1]["content"])["stage"] for body in model_service] == [
        "index_structure",
        "index_summary",
        "index_summary",
    ]


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        ('{"summaries":[],"summaries":[]}', "$.summaries: duplicate field"),
        (
            '{"summaries":[{"id":"1","summary":"first","summary":"second"}]}',
            "$.summaries[0].summary: duplicate field",
        ),
        ('{"summaries": [', "$: Expecting value: line 1 column 16 (char 15)"),
    ],
)
def test_summary_decode_rejection_retains_path_and_reason(
    kb_dir, tmp_path, monkeypatch, raw, detail
):
    path = tmp_path / "source.md"
    path.write_text("# Install\n\nUse version 7.")
    stages = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stages.append(payload["stage"])
        result = model_response(evidence_response(payload))
        if payload["stage"] == "index_summary":
            result.choices[0].message.content = raw
        return result

    monkeypatch.setattr(litellm, "completion", completion)
    source, parsed, settings, saved = prepare(kb_dir, path, {"summaries": True})
    reason = "index_summary_invalid: " + detail
    assert saved["status"] == "degraded"
    assert saved["reason"] == reason
    assert saved["windows"][0]["reason"] == reason
    with kb_ingest_lock(kb_dir / ".openkb"):
        resumed = prepare_navigation(
            kb_dir, source, parsed, settings, bundle=resolve_credential_bundle(kb_dir)
        )
    assert resumed["reason"] == reason
    assert stages == ["index_structure", "index_summary", "index_summary"]


@pytest.mark.parametrize("missing_child", [False, True])
def test_cross_window_parent_uses_child_hints_and_direct_introduction(
    kb_dir, tmp_path, model_service, missing_child
):
    path = tmp_path / "parent.md"
    path.write_text(
        "# Parent\n\nParent introduction and condition.\n\n## Child A\n\n"
        + "\n\n".join("ALREADY_SUMMARIZED_A " * 30 for _ in range(4))
        + "\n\n## Child B\n\n"
        + "\n\n".join("CHILD_B_BODY " * 30 for _ in range(4))
        + "\n\n# Next\n\nOther topic."
    )
    requests = []

    def respond(body):
        p = json.loads(body["messages"][-1]["content"])
        requests.append(p)
        if p["stage"] == "index_structure":
            return {"sections": []}
        return {
            "summaries": [
                {
                    "id": n["id"],
                    "summary": None
                    if missing_child and n["title"] == "Child B"
                    else n["title"] + " themes and conditions.",
                }
                for n in p["nodes"]
            ]
        }

    model_service.respond = respond
    source, parsed, settings, saved = prepare(
        kb_dir, path, {"summaries": True, "window_tokens": 600}
    )
    parent = next(n for n in saved["nodes"] if n["title"] == "Parent")
    merges = [p for p in requests if "summary_input" in p]
    assert len(merges) == 1
    p = merges[0]
    original = " ".join(b["text"] for b in p["evidence"]["blocks"])
    assert "Parent introduction and condition." in original
    assert "ALREADY_SUMMARIZED_A" not in original and "CHILD_B_BODY" not in original
    assert {n["title"] for n in p["summary_input"]["inputs"]} == {"Child A", "Child B"}
    details = parent["summary_details"]
    assert details["basis"] == "mixed"
    assert details["status"] == ("partial" if missing_child else "complete")
    child_b = next(n for n in saved["nodes"] if n["title"] == "Child B")
    if missing_child:
        assert all(right <= child_b["start"] for left, right in details["covered_ranges"])
    before = len(model_service)
    with kb_ingest_lock(kb_dir / ".openkb"):
        restored = prepare_navigation(
            kb_dir, source, parsed, settings, bundle=resolve_credential_bundle(kb_dir)
        )
    assert restored["id"] == saved["id"] and len(model_service) == before


def test_completed_summary_is_reused_when_targets_are_regrouped(kb_dir, tmp_path, model_service):
    from copy import deepcopy

    from openkb.agent.evidence_checkpoints import CompilationCheckpoints
    from openkb.navigation_enhancement import IndexAllowance
    from openkb.navigation_evidence import evidence_descriptor, read_evidence_group
    from openkb.navigation_requests import summarize_group
    from openkb.processing import processing_scope

    path = tmp_path / "regroup.md"
    path.write_text("# First\n\nOriginal one.\n\n# Second\n\nOriginal two.")
    source, parsed, settings, saved = prepare(kb_dir, path, {"summaries": False})
    evidence = read_evidence_group(
        kb_dir, source, parsed, evidence_descriptor(source, parsed, 0, len(parsed.blocks))
    )
    nodes = deepcopy(saved["nodes"][1:])
    for n in nodes:
        n.pop("summary_details", None)
    requests = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        requests.append([n["title"] for n in payload["nodes"]])
        return {
            "summaries": [
                {"id": n["id"], "summary": n["title"] + " hint."} for n in payload["nodes"]
            ]
        }

    model_service.respond = respond
    with (
        kb_ingest_lock(kb_dir / ".openkb"),
        processing_scope(settings) as budget,
        CompilationCheckpoints(
            kb_dir, source, parsed, settings, resolve_credential_bundle(kb_dir)
        ) as checkpoints,
    ):
        allowance = IndexAllowance(budget, {}, False)
        summarize_group(
            evidence,
            deepcopy(nodes[:1]),
            settings,
            resolve_credential_bundle(kb_dir),
            allowance,
            checkpoints,
            saved["profile"],
        )
        summarize_group(
            evidence,
            nodes,
            settings,
            resolve_credential_bundle(kb_dir),
            allowance,
            checkpoints,
            saved["profile"],
        )
    assert requests == [["First"], ["Second"]]
    assert all(n["summary_details"]["basis"] == "original" for n in nodes)


def test_parent_summary_inheritance_checks_child_dependencies():
    from copy import deepcopy

    from openkb.navigation_anchors import inherit_summaries
    from openkb.navigation_summary_inputs import child_signature

    child = {
        "id": "child",
        "parent": "parent",
        "title": "Child",
        "title_origin": "source",
        "start": 1,
        "end": 4,
        "summary": "A condition.",
        "summary_origin": "model",
        "summary_details": {
            "status": "complete",
            "reason": None,
            "covered_ranges": [[1, 4]],
            "basis": "original",
        },
    }
    parent = {
        "id": "parent",
        "parent": None,
        "title": "Parent",
        "title_origin": "source",
        "start": 0,
        "end": 4,
        "summary": "Parent hint.",
        "summary_origin": "model",
        "summary_details": {
            "status": "complete",
            "reason": None,
            "covered_ranges": [[0, 4]],
            "basis": "mixed",
            "input_signature": child_signature([child]),
        },
    }
    old = [parent, child]
    fresh = deepcopy(old)
    for n in fresh:
        n.update(summary="", summary_origin="unavailable")
        n.pop("summary_details")
    inherit_summaries(fresh, old)
    assert fresh == old
    changed = deepcopy(fresh)
    changed[0].update(summary="", summary_origin="unavailable")
    changed[0].pop("summary_details")
    changed[1]["title"] = "Corrected child title"
    inherit_summaries(changed, old)
    assert "summary_details" not in changed[0]
    assert changed[0]["summary"] == ""
