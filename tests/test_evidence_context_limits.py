"""Compilation boundaries exercised through the shared document operation."""

import json

import litellm
import yaml

from openkb.application.documents import import_document
from openkb.application.source_actions import continue_source
from tests.http_model_fixture import evidence_response


def test_small_output_budget_splits_fact_and_generation_batches(kb_dir, tmp_path, model_service):
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
    units = [unit for call in extracted if call["stage"] == "facts" for unit in call["units"]]
    assert len(units) == 30


def test_large_existing_catalog_leaves_room_for_new_evidence(kb_dir, tmp_path, model_service):
    for number in range(600):
        (kb_dir / f"wiki/concepts/catalog-{number:04}.md").write_text(f"# Catalog {number}\n")
    source = tmp_path / "small.md"
    source.write_text("Required version is 7.")
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(context_tokens=4096)
    config_path.write_text(yaml.safe_dump(config))
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(list((kb_dir / "wiki/concepts").glob("*.md"))) == 601


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
        response = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, output in zip(payload["units"], response["units"]):
                if not unit["text"].startswith("Execute"):
                    output.update(facts=[], empty_reason="Context for the command")
        if payload["stage"] == "generation":
            generated.append(payload)
        return response

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert generated and all("Linux version 7" in json.dumps(item) for item in generated)
    assert any(
        "Linux version 7" in context["text"] and context["reference"]["parse_id"] == result.parse_id
        for call in generated
        for passage in call["evidence"]
        for context in passage["neighbors"]
    )


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
    assert first.reason == "generated_asset_evidence_invalid"
    valid = True
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert len(model_service) == 4


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
    config["processing"].update(context_tokens=4096, max_requests=250, max_tokens=1000000)
    path.write_text(yaml.safe_dump(config))
    generated = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "generation":
            generated.extend(payload["evidence"])
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    table = [item for item in generated if "table" in item["location"]]
    assert table
    assert all("page" not in item["location"] for item in table)
    assert all("Required value" in item["context"] for item in table)
    assert len({item["location"]["row"] for item in table}) == 61
