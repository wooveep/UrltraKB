"""Publication must preserve prerequisites across topic and paragraph boundaries."""

import json
from collections import Counter

import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from openkb.config import DEFAULT_CONFIG
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize("long_context", [False, True])
def test_failed_prerequisite_withdraws_dependent_action_but_retains_independent_topic(
    kb_dir, tmp_path, monkeypatch, long_context
):
    config = {
        **DEFAULT_CONFIG,
        "model": "openai/offline-test",
        "navigation": {"enabled": False},
    }
    if long_context:
        config["processing"] = {
            **DEFAULT_CONFIG["processing"],
            "context_tokens": 16000,
            "output_tokens": 2048,
            "max_context_tokens": 64000,
            "max_output_tokens": 4096,
        }
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(config))
    source = tmp_path / "operations.md"
    source.write_text(
        "A verified backup is required before migration.\n\n"
        "Then run migrate --strict.\n\n"
        + "\n\n".join(
            f"Metrics endpoint {i} uses port 9342. Its collection timeout is 90 seconds."
            for i in range(40 if long_context else 1)
        )
    )
    calls = Counter()
    fixed = False

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        calls[stage] += 1
        value = evidence_response(payload) if stage != "dependencies" else None
        if stage == "facts":
            rows = []
            for row, unit in zip(value["units"], payload["units"], strict=True):
                topic = (
                    "Backup"
                    if "backup" in unit["text"]
                    else ("Migration" if "migrate" in unit["text"] else "Metrics")
                )
                row["facts"][0]["topic"] = topic
                if fixed or topic != "Backup":
                    rows.append(row)
            value = {"units": rows}
        elif stage == "planning":
            value = {
                "topics": [
                    {"name": label.lower(), "title": label, "kind": "concept", "members": [uid]}
                    for uid, label in payload["topic_labels"].items()
                ]
            }
        elif stage == "dependencies":
            assert any("backup" in block["text"] for block in payload["source"])
            value = {
                "topics": [
                    {
                        "path": topic["path"],
                        "status": "dependent"
                        if topic["path"] == "concepts/migration"
                        else "independent",
                        "reason": "Migration requires the excluded backup; metrics is independent.",
                    }
                    for topic in payload["candidates"]
                ]
            }
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    assert not (kb_dir / "wiki/concepts/migration.md").exists()
    assert any(row["reason"] == "required_context_omitted" for row in first.omissions)
    before = calls.copy()
    assert import_document(kb_dir, source).omissions == first.omissions
    assert calls == before
    unchanged = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert unchanged.knowledge_compilation == "completed", unchanged
    before = calls.copy()
    config["verification_thinking"] = "enabled"
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(config))
    reviewed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert reviewed.knowledge_compilation == "completed", reviewed
    assert calls["dependencies"] > before["dependencies"]
    assert not (kb_dir / "wiki/concepts/migration.md").exists()
    fixed = True
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert not resumed.omissions
    assert (kb_dir / "wiki/concepts/migration.md").exists()


@pytest.mark.parametrize("decision", ["malformed", "duplicate_status", "unknown", "dependent"])
def test_dependency_protocol_failure_can_resume_without_rerolling_valid_refusal(
    kb_dir, tmp_path, monkeypatch, decision
):
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["navigation"] = {"enabled": False}
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "operations.md"
    source.write_text("A verified backup is required.\n\nMetrics uses port 9342.")
    calls = Counter()
    repaired = False

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        stage = payload["stage"]
        calls[stage] += 1
        if stage == "dependencies":
            if decision == "malformed" and not repaired:
                return {"topics": []}  # Valid JSON, incomplete candidate coverage.
            if decision == "duplicate_status" and not repaired:
                path = json.dumps(payload["candidates"][0]["path"])
                return (
                    '{"topics":[{"path":'
                    + path
                    + ',"status":"dependent","status":"independent","reason":"Conflict."}]}'
                )
            return {
                "topics": [
                    {
                        "path": row["path"],
                        "status": "independent" if repaired else decision,
                        "reason": "The original prerequisite cannot be established.",
                    }
                    for row in payload["candidates"]
                ]
            }
        value = evidence_response(payload)
        if stage == "facts":
            value["units"] = [
                row
                for row, unit in zip(value["units"], payload["units"], strict=True)
                if "backup" not in unit["text"]
            ]
        return value

    def completion(**kwargs):
        value = respond(kwargs)
        result = response(value)
        if isinstance(value, str):
            result.choices[0].message.content = value
        return result

    monkeypatch.setattr(litellm, "completion", completion)
    invalid = decision in {"malformed", "duplicate_status"}
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "unfinished", first
    assert first.reason == (
        "dependency_invalid_response" if invalid else "dependency_scope_unresolved"
    )
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    before = calls.copy()
    repaired = True
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    if invalid:
        assert resumed.knowledge_compilation == "completed", resumed
        assert calls["dependencies"] == before["dependencies"] + 1
        assert list((kb_dir / "wiki/concepts").glob("*.md"))
    else:
        assert resumed.knowledge_compilation == "unfinished", resumed
        assert calls["dependencies"] == before["dependencies"]
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    assert calls["generation"] == before["generation"]
    assert calls["verification"] == before["verification"]
