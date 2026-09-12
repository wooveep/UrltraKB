"""Publish verified siblings while keeping excluded content explicit and recoverable."""

import json
from collections import Counter

import litellm
import pytest
import yaml

from openkb.application.documents import DocumentResult, import_document
from openkb.application.source_actions import continue_source
from openkb.config import DEFAULT_CONFIG
from openkb.processing import ProcessingIncomplete
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.fixture
def setup(kb_dir, tmp_path, monkeypatch):
    config = {
        **DEFAULT_CONFIG,
        "model": "openai/offline-test",
        "language": "en",
        "navigation": {"enabled": False},
        "processing": {**DEFAULT_CONFIG["processing"], "concurrency": 2},
    }
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(config))
    source = tmp_path / "manual.md"
    source.write_text("Alpha requirement.\n\nBeta requirement.")
    calls = Counter()
    state = {"stage": "verification", "broken": True, "global": None}

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        calls[stage] += 1
        if state["global"] and stage == "generation":
            raise state["global"]
        value = evidence_response(payload)
        if stage == "facts":
            for row, unit in zip(value["units"], payload["units"], strict=True):
                row["facts"][0]["topic"] = "Beta" if "Beta" in unit["text"] else "Alpha"
            if state["broken"] and state["stage"] == stage:
                value["units"] = [r for r in value["units"] if r["facts"][0]["topic"] != "Beta"]
        if stage == "planning":
            value = {
                "topics": [
                    {"name": title.lower(), "title": title, "kind": "concept", "members": [uid]}
                    for uid, title in payload["topic_labels"].items()
                    if not (state["broken"] and state["stage"] == stage and title == "Beta")
                ]
            }
        if stage == "generation":
            title = payload.get("title", payload.get("revision", {}).get("title", "Beta"))
            value = {
                "content": "# "
                + title
                + "\n"
                + title
                + " requirement.\nSee [[concepts/beta|Beta]].\n`[[concepts/beta]]`",
                "covered": [f["id"] for f in payload["facts"]],
            }
        if stage == "verification" and state["broken"] and state["stage"] == stage:
            if payload.get("title") == "Beta" or "# Beta" in payload.get("candidate", ""):
                value = {"verdict": "unsupported", "reason": "Unsupported claim."}
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    return source, state, calls


@pytest.mark.parametrize("stage", ["facts", "planning", "verification"])
def test_local_failure_publishes_only_verified_content(kb_dir, setup, stage):
    source, state, calls = setup
    state["stage"] = stage
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", (result.reason, result)
    assert result.omissions and result.omissions[0]["stage"] == (
        "generation" if stage == "verification" else stage
    )
    assert "knowledge_content_omitted" in result.warnings
    assert (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    body = (kb_dir / "wiki/concepts/alpha.md").read_text()
    assert "See Beta." in body
    assert "`[[concepts/beta]]`" in body
    summary = next((kb_dir / "wiki/summaries").glob("*.md")).read_text()
    assert "内容遗漏" in summary and "[[concepts/beta" not in summary
    assert DocumentResult.from_summary(json.loads(json.dumps(result.__dict__))) == result
    before = calls.copy()
    repeated = import_document(kb_dir, source)
    assert repeated.status == "skipped" and repeated.omissions == result.omissions
    assert calls == before


def test_explicit_continue_can_complete_excluded_work(kb_dir, setup):
    source, state, calls = setup
    state["stage"] = "facts"
    first = import_document(kb_dir, source)
    assert first.omissions
    state["broken"] = False
    second = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert second.knowledge_compilation == "completed", second
    assert not second.omissions
    assert (kb_dir / "wiki/concepts/beta.md").exists()
    assert "内容遗漏" not in next((kb_dir / "wiki/summaries").glob("*.md")).read_text()


@pytest.mark.parametrize(
    "error",
    [
        ProcessingIncomplete("request_budget_exhausted", "generation"),
        RuntimeError("service unavailable"),
    ],
)
def test_global_failure_does_not_become_content_omission(kb_dir, setup, error):
    source, state, calls = setup
    state["global"] = error
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation != "completed"
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_new_version_withdraws_old_contribution_for_excluded_topic(kb_dir, setup):
    source, state, calls = setup
    state["broken"] = False
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed"
    source.write_text("Alpha requirement updated.\n\nBeta requirement updated.")
    state["broken"] = True
    second = import_document(kb_dir, source)
    assert second.knowledge_compilation == "completed", second
    assert second.omissions
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert "See Beta." in (kb_dir / "wiki/concepts/alpha.md").read_text()
