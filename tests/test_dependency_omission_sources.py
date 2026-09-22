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


def test_import_binds_compute_recovery_to_its_original_global_prerequisite(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "recovery.md"
    source.write_text(
        "# Preparation\n\n" + PREPARATION + "\n\n"
        "# Management recovery\n\nRestore the management database.\n\n"
        "# Compute recovery\n\n" + JOIN + "\n\n"
        "# Metrics\n\nMetrics listens on port 9342."
    )
    compute_evidence = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        stage = request["stage"]
        value = evidence_response(request)
        if stage == "planning":
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

            preparation = ranges("Preparation", PREPARATION)
            management = ranges("Management recovery", "Restore the management database.")
            compute = ranges("Compute recovery", JOIN)
            metrics = ranges("Metrics", "Metrics listens on port 9342.")
            target = request["target"]
            return {
                "overview": {
                    "text": "Recovery procedures and prerequisites.",
                    "ranges": target.get(
                        "ranges", [[target["target_start"], target["target_end"]]]
                    ),
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "management",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/management-recovery",
                        "title": "Management recovery",
                        "purpose": "Restore the management database safely.",
                        "subject_ranges": management,
                        "necessary_context": [
                            {
                                "relation": "applicable_condition",
                                "ranges": preparation,
                                "basis": basis(preparation),
                                "basis_ranges": preparation,
                            }
                        ],
                    },
                    {
                        "local_key": "compute",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/compute-recovery",
                        "title": "Compute recovery",
                        "purpose": "Join the replacement compute node safely.",
                        "subject_ranges": compute,
                        "necessary_context": [
                            {
                                "relation": "applicable_condition",
                                "ranges": preparation,
                                "basis": basis(preparation),
                                "basis_ranges": preparation,
                            }
                        ],
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
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        elif stage == "generation":
            title = request["page"]["title"]
            content = {
                "Management recovery": "Incorrect management recovery.",
                "Compute recovery": PREPARATION + "\n\n" + JOIN,
                "Metrics": "Metrics listens on port 9342.",
            }[title]
            if title == "Compute recovery":
                compute_evidence.append(
                    "\n".join(row["text"] for row in request["evidence"]["blocks"])
                )
            return {
                "content": content,
                "covered": [row["id"] for row in request["occurrences"]],
            }
        if stage == "verification" and request["page"]["title"] == "Management recovery":
            return {
                "verdict": "unsupported",
                "reason": "The candidate contradicts the original management procedure.",
                "issues": ["The original requires restoring the database."],
            }
        if stage == "verification" and request["page"]["title"] == "Compute recovery":
            compute_evidence.append("\n".join(row["text"] for row in request["evidence"]["blocks"]))
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert compute_evidence and all(PREPARATION in evidence for evidence in compute_evidence)
    assert not (kb_dir / "wiki/concepts/management-recovery.md").exists()
    assert (kb_dir / "wiki/concepts/compute-recovery.md").exists()
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    previous = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert len(model_service) == previous
    assert (kb_dir / "wiki/concepts/compute-recovery.md").exists()


def test_unresolved_prerequisites_block_only_recovery_before_generation(
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
    stages = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        stages.append(request["stage"])
        value = evidence_response(request)
        if request["stage"] == "planning":
            blocks = request["evidence"]["blocks"]
            recovery = [row for row in blocks if "Metrics listens" not in row["text"]]
            metrics = [row for row in blocks if "Metrics listens" in row["text"]]
            target = request["target"]
            registered = {
                item["name"]: item
                for item in request["carry"]["page_register"]
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }

            def page(name, title, purpose, ranges):
                prior = registered.get(name)
                return {
                    "local_key": name.rsplit("/", 1)[-1],
                    "target_key": prior["key"] if prior else "",
                    "target": prior.get("target", "") if prior else "",
                    "kind": "concept",
                    "name": name,
                    "title": title,
                    "purpose": purpose,
                    "subject_ranges": ranges,
                    "necessary_context": [],
                }

            output = {
                "overview": {
                    "text": "Recovery prerequisites and metrics configuration.",
                    "ranges": target.get(
                        "ranges", [[target["target_start"], target["target_end"]]]
                    ),
                    "limitations": ["Recovery prerequisites remain unresolved."],
                },
                "page_changes": [],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
            if recovery:
                recovery_ranges = [[recovery[0]["order"], recovery[-1]["order"] + 1]]
                output["page_changes"].append(
                    page(
                        "concepts/recovery",
                        "Recovery",
                        "Perform recovery only after all prerequisites are known.",
                        recovery_ranges,
                    )
                )
                output["unresolved"].append(
                    {
                        "location": recovery_ranges,
                        "problem_type": "missing_prerequisite",
                        "missing_target": "complete recovery prerequisite material",
                        "affected_pages": ["recovery"],
                        "blocking": True,
                        "reason": "Recovery prerequisites exceed the available source material.",
                    }
                )
            if metrics:
                output["page_changes"].append(
                    page(
                        "concepts/metrics",
                        "Metrics",
                        "Metrics listener configuration.",
                        [[metrics[0]["order"], metrics[0]["order"] + 1]],
                    )
                )
            return output
        if request["stage"] == "generation":
            return {
                "content": "Metrics listens on port 9342.",
                "covered": [row["id"] for row in request["occurrences"]],
            }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert "planning" in stages
    assert "generation" in stages
    assert "verification" in stages
    assert "dependencies" not in stages
    assert result.coverage["status"] == "partial"
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    assert any(row.get("reason") == "unresolved_prerequisite_blocked" for row in result.omissions)
