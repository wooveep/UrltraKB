"""Joint omissions must not leave operations without either valid prerequisite."""

import json

import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response


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
    plans = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        value = evidence_response(request)
        if request["stage"] == "planning":
            plans.append(request)
            blocks = request["evidence"]["blocks"]

            def ranges(heading, body):
                return [
                    [row["order"], row["order"] + 1]
                    for row in blocks
                    if row["text"].lstrip("# ").strip() == heading or row["text"] == body
                ]

            def basis(selected):
                return "\n".join(
                    next(row["text"] for row in blocks if row["order"] == index)
                    for start, end in selected
                    for index in range(start, end)
                )

            certification = ranges(
                "Certification", "Certify the remote approver before issuing Remote approval."
            )
            local = ranges("Local approval", "Obtain a signed local approval.")
            remote = ranges("Remote approval", "Obtain a signed remote approval.")
            activation = ranges(
                "Activation", "Activate with either Local approval or Remote approval."
            )
            metrics = ranges("Metrics", "Metrics listens on port 9342.")
            target = request["target"]
            output = {
                "overview": {
                    "text": "Approval and activation requirements.",
                    "ranges": target.get(
                        "ranges", [[target["target_start"], target["target_end"]]]
                    ),
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "activation",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/activation",
                        "title": "Activation",
                        "purpose": "Activate after a valid approval.",
                        "subject_ranges": activation,
                        "necessary_context": (
                            [
                                {
                                    "relation": "explicit_reference",
                                    "ranges": remote,
                                    "basis": basis(remote),
                                    "basis_ranges": remote,
                                }
                            ]
                            if remote_state == "retained"
                            else []
                        ),
                    },
                    {
                        "local_key": "metrics",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/metrics",
                        "title": "Metrics",
                        "purpose": "Metrics listener configuration.",
                        "subject_ranges": metrics,
                        "necessary_context": [],
                    },
                ],
                "source_only": [
                    {
                        "ranges": local,
                        "reason": "Local approval is retained as source-only procedural detail.",
                    }
                ],
                "unresolved": [],
                "resolutions": [],
            }
            if remote_state == "retained":
                output["page_changes"].extend(
                    [
                        {
                            "local_key": "certification",
                            "target_key": "",
                            "target": "",
                            "kind": "concept",
                            "name": "concepts/certification",
                            "title": "Certification",
                            "purpose": "Certify the remote approver.",
                            "subject_ranges": certification,
                            "necessary_context": [],
                        },
                        {
                            "local_key": "remote",
                            "target_key": "",
                            "target": "",
                            "kind": "concept",
                            "name": "concepts/remote-approval",
                            "title": "Remote approval",
                            "purpose": "Obtain a remote approval after certification.",
                            "subject_ranges": remote,
                            "necessary_context": [
                                {
                                    "relation": "applicable_condition",
                                    "ranges": certification,
                                    "basis": basis(certification),
                                    "basis_ranges": certification,
                                }
                            ],
                        },
                    ]
                )
            else:
                output["source_only"].extend(
                    [
                        {
                            "ranges": certification,
                            "reason": (
                                "Unavailable remote-approval prerequisite is retained in source."
                            ),
                        },
                        {
                            "ranges": remote,
                            "reason": "Unavailable remote approval is retained in source.",
                        },
                    ]
                )
                output["unresolved"].append(
                    {
                        "location": activation,
                        "problem_type": "missing_prerequisite",
                        "missing_target": "a retained local or remote approval prerequisite",
                        "affected_pages": ["activation"],
                        "blocking": True,
                        "reason": (
                            "Activation cannot be published without one retained approval path."
                        ),
                    }
                )
            return output
        elif request["stage"] == "generation":
            title = request["page"]["title"]
            content = {
                "Certification": "Certify the remote approver before issuing Remote approval.",
                "Activation": "Activate with either Local approval or Remote approval.",
                "Remote approval": (
                    "Obtain a signed remote approval after certifying the remote approver."
                ),
                "Metrics": "Metrics listens on port 9342.",
            }[title]
            return {
                "content": content,
                "covered": [row["id"] for row in request["occurrences"]],
            }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert (kb_dir / "wiki/concepts/activation.md").exists() == (remote_state == "retained")
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    assert plans
    if remote_state == "retained":
        assert not result.omissions
    else:
        assert any(row["reason"] == "unresolved_prerequisite_blocked" for row in result.omissions)
    before = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert (kb_dir / "wiki/concepts/activation.md").exists() == (remote_state == "retained")
    assert len(model_service) == before
