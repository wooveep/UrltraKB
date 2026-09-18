"""Joint omissions must not leave operations without either valid prerequisite."""

import json

import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response
from tests.test_dependency_omission_sources import _expanded


@pytest.mark.parametrize("remote_state", ["initially_omitted", "withdrawn_later", "retained"])
def test_import_preserves_alternative_prerequisites_across_joint_omission_decisions(
    kb_dir, tmp_path, model_service, remote_state
):
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"]["max_requests"] = 100
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "alternative-prerequisites.md"
    source.write_text(
        "# Certification\n\nCertify the remote approver before issuing Remote approval.\n\n"
        "# Local approval\n\nObtain a signed local approval.\n\n"
        "# Remote approval\n\nObtain a signed remote approval.\n\n"
        "# Activation\n\nActivate with either Local approval or Remote approval.\n\n"
        "# Metrics\n\nMetrics listens on port 9342."
    )
    decisions = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        value = evidence_response(request)
        if request["stage"] == "facts":
            rows = []
            for unit, row in zip(request["units"], value["units"], strict=True):
                if (
                    unit["text"] == "Obtain a signed local approval."
                    or (
                        remote_state != "retained"
                        and unit["text"]
                        == "Certify the remote approver before issuing Remote approval."
                    )
                    or (
                        remote_state == "initially_omitted"
                        and unit["text"] == "Obtain a signed remote approval."
                    )
                ):
                    continue
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Organizational heading")
                else:
                    for fact in row["facts"]:
                        fact.update(
                            topic=unit["headings"][-1], statement=unit["text"], quote=unit["text"]
                        )
                rows.append(row)
            value = {"units": rows}
        elif request["stage"] == "planning":
            value = {
                "topics": [
                    {
                        "name": title.lower().replace(" ", "-"),
                        "title": title,
                        "kind": "concept",
                        "members": [uid],
                    }
                    for uid, title in request["topic_labels"].items()
                ]
            }
        elif request["stage"] == "generation":
            title = request.get("title") or request["revision"]["title"]
            content = {
                "Certification": "Certify the remote approver before issuing Remote approval.",
                "Activation": "Activate with either Local approval or Remote approval.",
                "Remote approval": "Obtain a signed remote approval.",
                "Metrics": "Metrics listens on port 9342.",
            }[title]
            for fragment in value.get("fragments", []):
                fragment["content"] = content
            if "content" in value:
                value["content"] = content
        elif request["stage"] == "dependencies":
            request = _expanded(request, request.get("context_pool", {}))
            omitted = {
                row["reference"]["block_id"]
                for gap in request["omissions"]
                for row in gap["source_references"]
            }
            missing_text = {
                row["text"] for row in request["source"] if row["reference"]["block_id"] in omitted
            }
            no_approval = {
                "Obtain a signed local approval.",
                "Obtain a signed remote approval.",
            } <= missing_text
            value = {"topics": []}
            for candidate in request["candidates"]:
                path = candidate["path"]
                decisions.append((path, no_approval))
                value["topics"].append(
                    {
                        "path": path,
                        "status": "dependent"
                        if (path == "concepts/activation" and no_approval)
                        or (
                            path == "concepts/remote-approval"
                            and "Certify the remote approver before issuing Remote approval."
                            in missing_text
                        )
                        else "independent",
                        "reason": "Activation requires at least one retained approval; "
                        "metrics has no approval prerequisite.",
                    }
                )
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert (kb_dir / "wiki/concepts/activation.md").exists() == (remote_state == "retained")
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    assert ("concepts/activation", remote_state != "retained") in decisions
    before = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert (kb_dir / "wiki/concepts/activation.md").exists() == (remote_state == "retained")
    assert all(
        json.loads(call["messages"][-1]["content"])["stage"] == "facts"
        for call in model_service[before:]
    )
