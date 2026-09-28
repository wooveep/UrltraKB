"""Exercise the original PDF algorithm through the saved-source/model/store interface."""

import json
from collections import Counter

import pymupdf
import pytest

from openkb.config import load_config, resolve_credential_bundle
from openkb.evidence import Evidence
from openkb.locks import kb_ingest_lock
from openkb.navigation import prepare_navigation, read_navigation
from openkb.pageindex_store import indexed_reader
from openkb.parsing import parse_document
from tests.http_model_fixture import ModelReply
from tests.test_native_parsing import source_version


def prepared(kb, tmp_path, texts):
    path = tmp_path / "original.pdf"
    with pymupdf.open() as document:
        for text in texts:
            document.new_page().insert_text((40, 40), text)
        document.save(path)
    source = source_version(kb, path)
    parsed = parse_document(kb, source, options={"ocr": {"policy": "off"}})
    settings = load_config(kb / ".openkb/config.yaml")
    settings["processing"].update(
        max_attempts=2, max_requests=120, max_tokens=2000000, output_tokens=4096
    )
    settings["navigation"] = {"enabled": True, "summaries": True}
    return source, parsed, settings


def build(kb, source, parsed, settings):
    with kb_ingest_lock(kb / ".openkb"):
        result = prepare_navigation(
            kb, source, parsed, settings, bundle=resolve_credential_bundle(kb)
        )
    return read_navigation(kb, source, identity=result["id"], limit=200)


def response(body):
    task = json.loads(body["messages"][-1]["content"])
    name = task.get("legacy_task")
    if name == "toc_detector_single_page":
        return {"toc_detected": "no"}
    if name == "generate_toc_init":
        return [
            {"structure": "1", "title": "Alpha", "physical_index": "<physical_index_1>"},
            {"structure": "1.1", "title": "Child", "physical_index": "<physical_index_2>"},
            {"structure": "2", "title": "Beta", "physical_index": "<physical_index_3>"},
            {"structure": "3", "title": "Delta", "physical_index": "<physical_index_3>"},
        ]
    if name == "check_title_appearance":
        return {"answer": "yes"}
    if name == "check_title_appearance_in_start":
        return {"start_begin": "no" if "title is Delta" in task["task_rules"] else "yes"}
    if name in {"generate_node_summary", "generate_doc_description"}:
        return ModelReply("A summary of the supplied document content.")
    raise AssertionError(name)


def contents_response(body):
    """Two printed-page entries; their physical PDF pages have an offset of two."""
    task = json.loads(body["messages"][-1]["content"])
    name = task["legacy_task"]
    if name == "toc_detector_single_page":
        return {"toc_detected": "yes" if "...." in task["evidence"]["pdf"]["text"] else "no"}
    if name == "detect_page_index":
        return {"page_index_given_in_toc": "yes"}
    if name == "toc_transformer":
        return {
            "table_of_contents": [
                {"structure": "1", "title": "Install", "page": 1},
                {"structure": "2", "title": "Repair", "page": 2},
            ]
        }
    if name == "check_if_toc_transformation_is_complete":
        return {"completed": "yes"}
    if name in {"toc_index_extractor", "add_page_number_to_toc"}:
        return [
            {"structure": "1", "title": "Install", "physical_index": "<physical_index_3>"},
            {"structure": "2", "title": "Repair", "physical_index": "<physical_index_4>"},
        ]
    return response(body)


def test_pdf_uses_original_tasks_and_preserves_own_page_ranges(kb_dir, tmp_path, model_service):
    source, parsed, settings = prepared(
        kb_dir,
        tmp_path,
        ["Alpha\nIntroduction.", "Child\nChild details.", "Beta\nBody.\nDelta\nMore details."],
    )
    model_service.respond = response
    saved = build(kb_dir, source, parsed, settings)
    assert saved["status"] == "enhanced", saved.get("reason")
    nodes = {n["title"]: n for n in saved["nodes"]}
    assert nodes["Child"]["parent"] == nodes["Alpha"]["id"]
    assert nodes["Alpha"]["end"] < nodes["Child"]["end"]
    assert (
        nodes["Beta"]["pdf_page_range"]
        == nodes["Delta"]["pdf_page_range"]
        == {"start": 3, "end": 3}
    )
    tasks = [json.loads(call["messages"][-1]["content"]) for call in model_service]
    counts = Counter(t["legacy_task"] for t in tasks)
    assert counts == {
        "toc_detector_single_page": 3,
        "generate_toc_init": 1,
        "check_title_appearance": 4,
        "check_title_appearance_in_start": 4,
        "generate_node_summary": 3,
        "generate_doc_description": 1,
    }
    same_page = [
        call
        for call, task in zip(model_service, tasks)
        if task["evidence"]["pdf"]["physical_pages"] == [1]
    ]
    assert len(same_page) >= 3
    assert len({call["messages"][-1]["content"].split(',"stage":')[0] for call in same_page}) == 1
    assert all(not t["evidence"]["blocks"] for t in tasks)
    reader = indexed_reader(kb_dir, source, parsed, saved)
    assert "Child" in "\n".join(
        reader.read(Evidence(source.source_id, source.id, parsed.id, b.id), max_chars=1000).text
        for b in parsed.blocks
    )
    calls = len(model_service)
    assert build(kb_dir, source, parsed, settings)["id"] == saved["id"]
    assert len(model_service) == calls
    from copy import deepcopy

    from openkb.agent.document_planning_admission import validate_navigation

    validate_navigation(saved, source, parsed)
    changed = deepcopy(saved)
    changed["nodes"][1]["pdf_page_range"]["end"] = 2
    with pytest.raises(ValueError, match="saved physical pages"):
        validate_navigation(changed, source, parsed)


def test_shared_page_summaries_have_one_executor(kb_dir, tmp_path, model_service):
    import time

    source, parsed, settings = prepared(
        kb_dir,
        tmp_path,
        ["Alpha\nIntroduction.", "Child\nChild details.", "Beta\nBody.\nDelta\nMore details."],
    )
    shared_calls = []

    def respond(body):
        task = json.loads(body["messages"][-1]["content"])
        if task["legacy_task"] == "generate_node_summary" and task["evidence"]["pdf"][
            "physical_pages"
        ] == [3]:
            shared_calls.append(len(shared_calls) + 1)
            variant = shared_calls[-1]
            time.sleep(0.1)
            return ModelReply(f"Same page summary, model wording variant {variant}.")
        return response(body)

    model_service.respond = respond
    saved = build(kb_dir, source, parsed, settings)
    nodes = {n["title"]: n for n in saved["nodes"]}
    assert saved["status"] == "enhanced"
    assert shared_calls == [1]
    assert nodes["Beta"]["summary"] == nodes["Delta"]["summary"]


def test_pdf_empty_summary_keeps_tree_and_other_summaries(kb_dir, tmp_path, model_service):
    source, parsed, settings = prepared(
        kb_dir,
        tmp_path,
        ["Alpha\nIntroduction.", "Child\nChild details.", "Beta\nBody.\nDelta\nMore details."],
    )

    def respond(body):
        task = json.loads(body["messages"][-1]["content"])
        if task["legacy_task"] == "generate_node_summary" and task["evidence"]["pdf"][
            "physical_pages"
        ] == [2]:
            return ModelReply("")
        return response(body)

    model_service.respond = respond
    saved = build(kb_dir, source, parsed, settings)
    assert saved["status"] == "degraded"
    assert len(saved["nodes"]) == 5
    child = next(n for n in saved["nodes"] if n["title"] == "Child")
    assert child["summary"] == "" and child["summary_details"]["status"] == "insufficient_evidence"
    assert any(n["summary"] for n in saved["nodes"] if n is not child)


def test_pdf_provider_error_is_not_relabelled_as_content(kb_dir, tmp_path, monkeypatch):
    from openkb.pdf_navigation_requests import PDFRequests

    source, parsed, settings = prepared(kb_dir, tmp_path, ["Alpha\nBody."])

    def failed(*args, **kwargs):
        raise ConnectionError("offline provider")

    monkeypatch.setattr(PDFRequests, "call", failed)
    with pytest.raises(ConnectionError, match="offline provider"):
        build(kb_dir, source, parsed, settings)
