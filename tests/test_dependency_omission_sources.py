"""Withdrawn topics retain their original conditions at the model transport boundary."""

import json

import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response

PREPARATION = "Before joining a replacement node, shut down all virtual machines."
JOIN = "Join the replacement compute node to the existing cluster."


def _expanded(value, pool):
    if isinstance(value, dict):
        if set(value) == {"context_ref"}:
            return _expanded(pool[value["context_ref"]], pool)
        return {key: _expanded(item, pool) for key, item in value.items()}
    if isinstance(value, list):
        return [_expanded(item, pool) for item in value]
    return value


def _references(value):
    if isinstance(value, dict):
        if {"source_id", "version_id", "parse_id", "block_id"} <= value.keys():
            yield value
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def test_import_binds_withdrawn_mixed_topic_to_its_original_global_prerequisite(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "recovery.md"
    source.write_text(
        "# Preparation\n\n" + PREPARATION + "\n\n"
        "# Management recovery\n\nRestore the management database.\n\n"
        "# Compute recovery\n\n" + JOIN + "\n\n"
        "# Metrics\n\nMetrics listens on port 9342."
    )
    dependency_inputs = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        stage = request["stage"]
        value = evidence_response(request)
        if stage == "facts":
            for unit, row in zip(request["units"], value["units"], strict=True):
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Organizational heading")
                else:
                    row["facts"] = [
                        {
                            "topic": unit["headings"][-1],
                            "statement": unit["text"],
                            "quote": unit["text"],
                        }
                    ]
        elif stage == "planning":
            ids = {title: uid for uid, title in request["topic_labels"].items()}
            value = {
                "topics": [
                    {
                        "name": "management-recovery",
                        "title": "Management recovery",
                        "kind": "concept",
                        "members": [ids["Preparation"], ids["Management recovery"]],
                    },
                    {
                        "name": "compute-recovery",
                        "title": "Compute recovery",
                        "kind": "concept",
                        "members": [ids["Compute recovery"]],
                    },
                    {
                        "name": "metrics",
                        "title": "Metrics",
                        "kind": "concept",
                        "members": [ids["Metrics"]],
                    },
                ]
            }
        elif stage == "generation":
            title = request.get("title") or request["revision"]["title"]
            content = {
                "Management recovery": "Incorrect management recovery.",
                "Compute recovery": JOIN,
                "Metrics": "Metrics listens on port 9342.",
            }[title]
            if "fragments" in value:
                for fragment in value["fragments"]:
                    fragment["content"] = content
            else:
                value["content"] = content
        elif stage == "verification" and request["title"] == "Management recovery":
            value = {
                "verdict": "unsupported",
                "reason": "The candidate contradicts the original management procedure.",
                "issues": [
                    {
                        "kind": "claim",
                        "candidate": "Incorrect management recovery.",
                        "occurrences": [request["occurrences"][0]["id"]],
                        "reason": "The original requires restoring the database.",
                    }
                ],
            }
        elif stage == "dependencies":
            expanded = _expanded(request, request.get("context_pool", {}))
            missing = next(
                (
                    row
                    for row in expanded["omissions"]
                    if "concepts/management-recovery" in row.get("items", [])
                ),
                None,
            )
            paths = {row["path"] for row in expanded["candidates"]}
            located = False
            if missing is not None and "concepts/compute-recovery" in paths:
                dependency_inputs.append(expanded)
                originals = [row for row in expanded["source"] if row["text"] == PREPARATION]
                refs = list(_references(missing))
                located = any(
                    ref == original["reference"] for ref in refs for original in originals
                )
            value = {
                "topics": [
                    {
                        "path": path,
                        "status": "dependent"
                        if path == "concepts/compute-recovery" and located
                        else "independent",
                        "reason": "Compute rejoin depends on the withdrawn preparation; "
                        "the unrelated metric does not.",
                    }
                    for path in paths
                ]
            }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert dependency_inputs, "The withdrawn topic must be reviewed against compute rejoin."
    for request in dependency_inputs:
        missing = next(
            row
            for row in request["omissions"]
            if "concepts/management-recovery" in row.get("items", [])
        )
        original = next(row for row in request["source"] if row["text"] == PREPARATION)
        assert original["location"] == {"kind": "text", "line": 3}
        assert original["reference"] in list(_references(missing)), (
            "An omitted topic name is insufficient: its original global prerequisite "
            "must be bound to the same source/version/parse/block in the dependency request."
        )
    assert not (kb_dir / "wiki/concepts/management-recovery.md").exists()
    assert not (kb_dir / "wiki/concepts/compute-recovery.md").exists()
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    previous = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert len(model_service) == previous
    assert not (kb_dir / "wiki/concepts/compute-recovery.md").exists()


def test_import_checks_required_omission_reference_capacity_before_generation(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "many-prerequisites.md"
    source.write_text(
        "# Recovery\n\n"
        + "\n\n".join(f"Missing preparation requirement {i}." for i in range(48))
        + "\n\nMetrics listens on port 9342."
    )
    config_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config_path.read_text())
    settings["processing"].update(
        context_tokens=8192,
        max_context_tokens=8192,
        output_tokens=1024,
        max_output_tokens=1024,
        max_requests=300,
        max_tokens=2000000,
        stage_timeout=120,
        document_timeout=180,
    )
    config_path.write_text(yaml.safe_dump(settings))
    stages, events = [], []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        stages.append(request["stage"])
        value = evidence_response(request)
        if request["stage"] == "facts":
            rows = []
            for unit, row in zip(request["units"], value["units"], strict=True):
                if "Missing preparation" in unit["text"]:
                    continue
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Organizational heading")
                rows.append(row)
            value = {"units": rows}
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source, on_event=events.append)
    assert result.knowledge_compilation == "completed", result
    assert "planning" in stages
    assert any(event.get("operation") == "capacity_preflight" for event in events), events
    assert "generation" not in stages
    assert "verification" not in stages
    assert "dependencies" not in stages
    assert any(
        row.get("reason") == "dependency_context_exceeds_request_budget" for row in result.omissions
    )
