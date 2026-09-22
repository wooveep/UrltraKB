"""Compilation boundaries exercised through the shared document operation."""

import json

import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response


@pytest.mark.parametrize("maximum", [4096, 32768])
def test_required_operation_context_uses_configured_capacity_before_omission(
    kb_dir, tmp_path, model_service, maximum
):
    source = tmp_path / "operation.md"
    source.write_text(
        "# Standby recovery\n\nExecute deploy --timeout 42.\n\n"
        + "\n\n".join(
            f"Background note {number}. "
            + "Retain this original context for the complete operation. " * 25
            for number in range(20)
        )
    )
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(
        context_tokens=4096,
        max_context_tokens=maximum,
        output_tokens=1024,
        max_output_tokens=1024,
        max_requests=100,
        max_tokens=1000000,
    )
    config_path.write_text(yaml.safe_dump(config))
    generated = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            target = payload["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            frozen_ranges = [
                [block["order"], block["order"] + 1] for block in payload["evidence"]["blocks"]
            ]

            def basis(selected):
                return "\n".join(
                    next(
                        block["text"]
                        for block in payload["evidence"]["blocks"]
                        if block["order"] == index
                    )
                    for start, end in selected
                    for index in range(start, end)
                )

            operation = next(
                (
                    block
                    for block in payload["evidence"]["blocks"]
                    if block["text"].startswith("Execute deploy --timeout 42.")
                ),
                None,
            )
            registered = payload["carry"]["page_register"]
            assert operation or registered
            return {
                "overview": {
                    "text": "Standby recovery deployment procedure.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "operation",
                        "target_key": registered[0]["key"] if registered else "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/standby-recovery",
                        "title": "Standby Recovery",
                        "purpose": "Run the standby recovery deployment operation.",
                        # Each serial planner increment may add only its own T
                        # as body and its frozen W as necessary context. This
                        # accumulates the complete operation without claiming
                        # a later window as already-read evidence.
                        "subject_ranges": ranges,
                        "necessary_context": (
                            [
                                {
                                    "relation": "applicable_condition",
                                    "ranges": frozen_ranges,
                                    "basis": basis(frozen_ranges),
                                    "basis_ranges": frozen_ranges,
                                }
                            ]
                            if not registered
                            else []
                        ),
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        output = evidence_response(payload)
        if payload["stage"] == "generation":
            generated.append(payload)
            output["content"] = "Execute deploy --timeout 42."
        return output

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed"
    if maximum == 4096:
        assert not generated
        assert any(
            row["reason"] == "planned_page_evidence_exceeds_request_budget"
            for row in result.omissions
        )
    else:
        assert generated
        assert list((kb_dir / "wiki/concepts").glob("*.md"))
        requests = [
            (call, json.loads(call["messages"][-1]["content"]))
            for call in model_service
            if json.loads(call["messages"][-1]["content"])["stage"]
            in {"generation", "verification"}
        ]
        assert {payload["stage"] for _, payload in requests} == {"generation", "verification"}
        for call, payload in requests:
            text = json.dumps(payload)
            assert all(f"Background note {number}." in text for number in range(20))
            assert "Execute deploy --timeout 42." in text
            assert call["max_tokens"] == 1024
        before = len(model_service)
        resumed = continue_source(kb_dir, result.source_id, version_id=result.input_version)
        assert resumed.knowledge_compilation == "completed"
        assert len(model_service) == before


def test_small_output_budget_keeps_formal_responses_within_limit(kb_dir, tmp_path, model_service):
    source = tmp_path / "parameters.md"
    source.write_text("\n\n".join(f"Parameter {i}: value 37." for i in range(30)))
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(context_tokens=128000, output_tokens=256, max_requests=100)
    config_path.write_text(yaml.safe_dump(config))
    sizes = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        sizes.append(litellm.token_counter(model=body["model"], text=json.dumps(response)))
        return response

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert max(sizes) <= 256
    extracted = [json.loads(call["messages"][-1]["content"]) for call in model_service]
    assert [call["stage"] for call in extracted] == ["planning", "generation", "verification"]
    assert len(extracted[0]["evidence"]["blocks"]) == 30


def test_large_existing_catalog_is_projected_before_planning_request(
    kb_dir, tmp_path, model_service
):
    for number in range(600):
        (kb_dir / f"wiki/concepts/catalog-{number:04}.md").write_text(
            f"# Catalog {number}\n" + "Catalog detail. " * 20
        )
    source = tmp_path / "small.md"
    source.write_text("Required version is 7.")
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(context_tokens=8192)
    config_path.write_text(yaml.safe_dump(config))
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    planning = next(
        json.loads(call["messages"][-1]["content"])
        for call in model_service
        if json.loads(call["messages"][-1]["content"])["stage"] == "planning"
    )
    assert len(planning["existing_targets"]) < 600
    assert len(planning["existing_pages"].splitlines()) < 600
    assert len(list((kb_dir / "wiki/concepts").glob("catalog-*.md"))) == 600


def test_distant_heading_conditions_are_reread_as_generation_evidence(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "conditions.md"
    source.write_text(
        "# Applies only to Linux version 7\n\nBackground one.\n\nBackground two.\n\n"
        "Execute deploy --timeout 42."
    )
    generated = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            blocks = payload["evidence"]["blocks"]
            target = payload["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            heading = next(block for block in blocks if "Linux version 7" in block["text"])
            command = next(block for block in blocks if block["text"].startswith("Execute"))
            background = [
                block
                for block in blocks
                if block["order"] not in {heading["order"], command["order"]}
            ]
            return {
                "overview": {
                    "text": "Linux-specific standby recovery procedure.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "deployment",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/standby-deployment",
                        "title": "Standby Deployment",
                        "purpose": "Run the deployment command safely.",
                        "subject_ranges": [[command["order"], command["order"] + 1]],
                        "necessary_context": [
                            {
                                "relation": "applicable_condition",
                                "ranges": [[heading["order"], heading["order"] + 1]],
                                "basis": heading["text"],
                                "basis_ranges": [[heading["order"], heading["order"] + 1]],
                            }
                        ],
                    }
                ],
                "source_only": [
                    {
                        "ranges": [[block["order"], block["order"] + 1] for block in background],
                        "reason": "Background prose does not add an operation or condition.",
                    }
                ],
                "unresolved": [],
                "resolutions": [],
            }
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            generated.append(payload)
        return response

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert generated and all("Linux version 7" in json.dumps(item) for item in generated)
    assert any(
        "Linux version 7" in context["text"]
        and context["reference"]["parse_id"]
        and any(route["route"] == "context_only" for route in context["routes"])
        for call in generated
        for context in call["evidence"]["blocks"]
    )
    pages = "\n".join(path.read_text() for path in (kb_dir / "wiki/concepts").glob("*.md"))
    assert result.parse_id in pages  # Wire identities are rebound before publication.


def test_invalid_figure_output_is_not_reused_after_model_correction(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "figure.md"
    source.write_text("Valve rated 37 kPa.")
    valid = False

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation" and not valid:
            response["content"] = "![Valve](asset:invented)"
        return response

    model_service.respond = respond
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed"
    assert any(row["reason"] == "document_generation_incomplete" for row in first.omissions)
    valid = True
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert len(model_service) == 5


def test_docx_table_parts_keep_headers_and_original_row_locations(kb_dir, tmp_path, model_service):
    from tests.document_fixtures import write_docx

    source = tmp_path / "table.docx"

    def cell(text):
        return f"<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"

    rows = [
        "<w:tr><w:trPr><w:tblHeader/></w:trPr>"
        + cell("Parameter")
        + cell("Required value")
        + "</w:tr>"
    ]
    rows.extend(
        "<w:tr>" + cell(f"timeout-{i}") + cell(f"{i + 37} seconds") + "</w:tr>" for i in range(60)
    )
    write_docx(
        source,
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>Linux version 7</w:t></w:r></w:p><w:tbl>" + "".join(rows) + "</w:tbl>",
    )
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"].update(
        context_tokens=32768,
        max_requests=250,
        max_tokens=1000000,
        stage_timeout=180,
        document_timeout=240,
    )
    path.write_text(yaml.safe_dump(config))
    generated = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "generation":
            generated.extend(payload["evidence"]["blocks"])
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    if result.reason == "request_budget_exhausted":
        # Required semantic reviews share the fixed allowance. Continue from
        # verified contributions without increasing the per-run request cap.
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
        result = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert result.knowledge_compilation == "completed", result
    table = [item for item in generated if "table" in item["location"]]
    assert table
    assert all("page" not in item["location"] for item in table)
    assert all(
        {"text": "Required value", "row": 1, "cell": 2, "relation": "declared_header"}
        in item["context_data"]["source_excerpts"]
        for item in table
        if item["location"]["row"] != 1
    )
    assert all(item["reference"]["parse_id"] for item in table)
    for item in table:
        if item["location"]["row"] == 1:
            expected = "Parameter" if item["location"]["cell"] == 1 else "Required value"
            assert expected in item["text"]
    assert len({item["location"]["row"] for item in table}) == 61
