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
    assert stages == ["index_structure", "index_summary"]
