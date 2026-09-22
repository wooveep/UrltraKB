"""Exercise import and original-source retrieval through the real agent tool loop."""

import asyncio
import json

from openkb.application.conversations import ask_question, continue_conversation
from openkb.application.documents import import_document
from openkb.application.source_history import source_status
from tests.http_model_fixture import evidence_response


def test_question_file_reads_are_bounded_and_paginate_without_losing_text(
    kb_dir, tmp_path, model_service
):
    from openkb.locks import atomic_write_text

    source = tmp_path / "limits.md"
    source.write_text("Pressure is 37 kPa.")
    assert import_document(kb_dir, source).knowledge_compilation == "completed"
    text = "Indexed source context. " * 1600 + "Final requirement: backup first."
    atomic_write_text(kb_dir / "wiki/concepts/manual.md", text)
    seen = []

    def chat(body):
        completed = [row for row in body["messages"] if row["role"] == "tool"]
        offset = 0
        if completed:
            window = json.loads(completed[-1]["content"])
            assert len(window["text"]) <= 16000
            seen.append(window["text"])
            offset = window["next_offset"]
            if offset is None:
                return {"role": "assistant", "content": "Backup first."}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read_" + str(len(completed)),
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "concepts/manual.md", "offset": offset}),
                    },
                }
            ],
        }

    model_service.chat_response = chat
    answer = asyncio.run(ask_question(kb_dir, "What is the final requirement in the manual?"))
    assert answer.status == "completed", answer
    assert "".join(seen) == text


def test_text_compiles_with_frozen_tree_and_query_and_chat_read_its_original(
    kb_dir, tmp_path, model_service, monkeypatch
):
    from pageindex.collection import Collection

    indexed_reads = []
    sdk_read = Collection.get_page_content

    def read_index(documents, doc_id, pages):
        result = sdk_read(documents, doc_id, pages)
        indexed_reads.append((doc_id, result))
        return result

    monkeypatch.setattr(Collection, "get_page_content", read_index)
    source = tmp_path / "handbook.md"
    source.write_text(
        "# Start\n\nCheck the backup first.\n\n## Pressure\n\nSet pressure to 37 kPa."
    )
    planned_evidence = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload.get("stage") == "planning":
            blocks = payload["evidence"]["blocks"]
            assert blocks, "Document planning must receive original source evidence"
            planned_evidence.append(blocks)
        return evidence_response(payload)

    model_service.respond = respond
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    assert indexed_reads, "Navigation must read the frozen source tree from PageIndex"
    assert planned_evidence
    assert all("navigation" not in block for batch in planned_evidence for block in batch)
    nav = source_status(kb_dir, imported.source_id)["navigation"]
    assert nav["parse"] == imported.parse_id
    assert nav["nodes"] and nav["nodes"][0]["end"] == 4
    seen = []

    def chat(body):
        completed = [row for row in body["messages"] if row["role"] == "tool"]
        tools = {tool["function"]["name"] for tool in body["tools"]}
        assert {"list_sources", "read_source_tree", "read_source_node"} <= tools
        if not completed:
            name, args = "list_sources", {"offset": 0, "limit": 20}
        elif len(completed) == 1:
            listing = json.loads(completed[-1]["content"])
            assert listing["sources"][0]["source_id"] == imported.source_id
            name, args = (
                "read_source_tree",
                {"source_id": imported.source_id, "offset": 0, "limit": 20},
            )
        elif len(completed) == 2:
            tree = json.loads(completed[-1]["content"])
            assert tree["id"] == nav["id"]
            name, args = (
                "read_source_node",
                {
                    "source_id": imported.source_id,
                    "node_id": tree["nodes"][0]["id"],
                    "offset": 0,
                    "start": 0,
                    "max_chars": 4000,
                },
            )
        else:
            text = json.loads(completed[-1]["content"])
            pressure = next(row for row in text["evidence"] if "37 kPa" in row["text"])
            assert pressure["reference"]["parse_id"] == imported.parse_id
            assert pressure["location"]["line"] == 7
            seen.append(pressure)
            return {
                "role": "assistant",
                "content": "Set pressure to 37 kPa. " + pressure["citation"],
            }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_" + str(len(completed)),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }

    model_service.chat_response = chat
    model_service.usage = {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "total_tokens": 130,
        "prompt_tokens_details": {"cached_tokens": 40},
    }
    for operation in (ask_question, continue_conversation):
        indexed_reads.clear()
        requests_before = len(model_service)
        answer = asyncio.run(operation(kb_dir, "What pressure should I set?"))
        assert answer.status == "completed", answer
        assert {doc_id for doc_id, _ in indexed_reads} == {nav["pageindex"]["doc_id"]}
        assert indexed_reads[0][1][-1]["content"] == "Set pressure to 37 kPa."
        assert "37 kPa" in answer.answer
        # Three retrieval calls and one final answer; there is no separate review call.
        assert len(model_service) - requests_before == 4
        assert answer.usage["observable_attempts"] == 4
        assert answer.usage["charged_tokens"] == 520
        assert all(
            row["cache_read_tokens"] == 40 for row in answer.usage["measurement"]["requests"]
        )
    assert len(seen) == 2


def test_index_summary_runs_before_compilation_with_shared_request_accounting(
    kb_dir, tmp_path, model_service
):
    import yaml

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["navigation"] = {
        "enabled": True,
        "processing": {**config["processing"], "max_requests": 2},
    }
    config["processing"]["max_requests"] = 6
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "summary.md"
    source.write_text("# Operations\n\n" + "Check each backup before migration. " * 18)
    stages = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        stages.append(payload["stage"])
        if payload["stage"] == "index_summary":
            return {
                "summaries": [
                    {"id": item["id"], "summary": "Backup operations."} for item in payload["nodes"]
                ]
            }
        return evidence_response(payload)

    model_service.respond = respond
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    assert stages[:2] == ["index_structure", "index_summary"]
    assert stages.count("index_summary") == 1
    assert stages.count("index_summary_verification") == 0
    assert imported.usage["observable_attempts"] == len(stages)
    nav = source_status(kb_dir, imported.source_id)["navigation"]
    assert nav["status"] == "enhanced"
    assert any(node["summary_origin"] == "model" for node in nav["nodes"])


def test_native_markdown_heading_hierarchy_preserves_valid_skipped_levels(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "levels.md"
    source.write_text(
        "# Main\n\nRequired backup.\n\n#### Detail\n\nCheck it.\n\n## Next\n\nProceed."
    )
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    nav = source_status(kb_dir, imported.source_id)["navigation"]
    nodes = nav["nodes"]
    assert nodes[2]["parent"] == nodes[1]["id"]
    assert nodes[3]["parent"] == nodes[1]["id"]
    assert nodes[2]["end"] == nodes[3]["start"]
    assert nodes[1]["end"] == 6


def test_unstructured_original_gets_bounded_inferred_ranges_and_rejects_unknown_blocks(
    kb_dir, tmp_path, model_service
):
    import yaml

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["navigation"] = {"enabled": True}
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "narrative.txt"
    source.write_text(
        "Verify backups before starting. " * 10 + "\n\n" + "The metrics endpoint uses 9342. " * 10
    )
    requests = []
    broken = False

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "index_structure":
            requests.append(payload)
            blocks = payload["evidence"]["blocks"]
            return {
                "sections": [
                    {
                        "start_block": "invented" if broken else block["id"],
                        "anchor": block["text"][:100],
                        "title_origin": "inferred",
                        "level": 1,
                        "title": "Derived section",
                    }
                    for block in blocks
                ]
            }
        if payload["stage"] == "index_location":
            return {"locations": [{"id": c["id"], "location": None} for c in payload["candidates"]]}
        if payload["stage"] == "index_summary":
            return {
                "summaries": [
                    {"id": row["id"], "summary": "Navigation only."} for row in payload["nodes"]
                ]
            }
        return evidence_response(payload)

    model_service.respond = respond
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    assert len(requests) == 1
    nav = source_status(kb_dir, first.source_id)["navigation"]
    assert any(node["structure_origin"] == "inferred" for node in nav["nodes"])
    broken = True
    source.write_text(source.read_text() + "\n\nNever bypass verification.")
    second = import_document(kb_dir, source)
    assert second.knowledge_compilation == "completed", second
    nav = source_status(kb_dir, second.source_id)["navigation"]
    assert nav["status"] == "degraded"
    assert nav["reason"] == "index_unlocated_sections"
    assert all(node["structure_origin"] != "inferred" for node in nav["nodes"])
    assert len(requests) == 2


def test_rebuilt_index_changes_query_binding_without_recompiling_original(
    kb_dir, tmp_path, model_service
):
    import yaml

    from openkb.application.source_actions import rebuild_source_navigation
    from tests.http_model_fixture import original_source_answer

    source = tmp_path / "rebuild.md"
    source.write_text("# Required pressure\n\n" + "Keep pressure at 37 kPa. " * 18)
    imported = import_document(kb_dir, source)
    old = source_status(kb_dir, imported.source_id)["navigation"]["id"]
    before = {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()}
    source.unlink()
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["navigation"] = {"enabled": True}
    config_path.write_text(yaml.safe_dump(config))
    stages = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        stages.append(payload.get("stage"))
        if payload.get("stage") == "index_summary":
            return {
                "summaries": [
                    {"id": row["id"], "summary": "Pressure guidance."} for row in payload["nodes"]
                ]
            }
        return evidence_response(payload)

    model_service.respond = respond
    rebuilt = rebuild_source_navigation(
        kb_dir, imported.source_id, version_id=imported.input_version, parse_id=imported.parse_id
    )
    assert "facts" not in stages and "generation" not in stages
    assert {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()} == before
    assert rebuilt["id"] != old
    seen = []

    def inspect(tree, result):
        assert tree["id"] == rebuilt["id"]
        assert "37 kPa" in " ".join(row["text"] for row in result["evidence"])
        seen.append(tree["id"])
        return "Pressure is 37 kPa."

    model_service.chat_response = original_source_answer(inspect)
    assert asyncio.run(ask_question(kb_dir, "What is the pressure?")).status == "completed"
    assert seen == [rebuilt["id"]]


def test_unpublished_new_version_never_replaces_original_used_by_query(
    kb_dir, tmp_path, model_service
):
    from openkb.application.execution import ExecutionContext
    from tests.http_model_fixture import original_source_answer

    source = tmp_path / "changing.md"
    source.write_text("Pressure is 37 kPa.")
    first = import_document(kb_dir, source)
    old = source_status(kb_dir, first.source_id)["navigation"]["id"]
    stopped = False

    def event(value):
        nonlocal stopped
        if value.get("stage") == "compiling":
            stopped = True

    source.write_text("Pressure is 99 kPa.")
    second = import_document(
        kb_dir, source, context=ExecutionContext(cancelled=lambda: stopped, on_event=event)
    )
    assert second.knowledge_compilation == "stopped", second
    assert second.parse_id != first.parse_id
    seen = []

    def inspect(tree, result):
        assert tree["id"] == old
        assert all(row["reference"]["parse_id"] == first.parse_id for row in result["evidence"])
        text = " ".join(row["text"] for row in result["evidence"])
        assert "37 kPa" in text and "99 kPa" not in text
        seen.append(tree["id"])
        return "Pressure is 37 kPa."

    model_service.chat_response = original_source_answer(inspect)
    for operation in (ask_question, continue_conversation):
        assert asyncio.run(operation(kb_dir, "What pressure is published?")).status == "completed"
    assert seen == [old, old]


def test_json_navigation_cannot_replace_the_pageindex_database(kb_dir, tmp_path, model_service):
    import pytest

    from openkb.sources import content_id
    from openkb.state import HashRegistry

    original = tmp_path / "pressure.md"
    original.write_text("The threshold is 37 kPa.")
    result = import_document(kb_dir, original)
    nav = source_status(kb_dir, result.source_id)["navigation"]
    legacy = {
        key: nav[key]
        for key in (
            "source_id",
            "version",
            "parse",
            "profile",
            "status",
            "reason",
            "positions",
            "usage",
            "nodes",
        )
    }
    legacy["schema"] = 2
    identity = content_id(legacy)
    path = kb_dir / ".openkb/source-store/navigation" / (identity + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy))
    registry = HashRegistry(kb_dir / ".openkb/hashes.json")
    entry = registry.get(result.source_id)
    registry.add(result.source_id, {**entry, "navigation_id": identity})
    database = kb_dir / ".openkb/pageindex.db"
    database.unlink()
    requests = len(model_service)
    with pytest.raises(ValueError, match="PageIndex"):
        asyncio.run(ask_question(kb_dir, "What is the threshold?"))
    assert len(model_service) == requests
    assert not database.exists()
    assert json.loads(path.read_text()) == legacy


def test_pdf_threshold_does_not_change_native_query_route(kb_dir, tmp_path, model_service):
    import pymupdf
    import yaml

    from tests.http_model_fixture import original_source_answer

    for threshold in (1, 100):
        path = kb_dir / ".openkb/config.yaml"
        settings = yaml.safe_load(path.read_text())
        settings["pageindex_threshold"] = threshold
        path.write_text(yaml.safe_dump(settings))
        original = tmp_path / f"manual-{threshold}.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((50, 50), "Prerequisite: finish the backup.")
            pdf.new_page().insert_text((50, 50), "Only then migrate; timeout is 42 seconds.")
            pdf.save(original)
        result = import_document(kb_dir, original)
        assert result.knowledge_compilation == "completed", result

    def inspect(tree, result):
        rows = result["evidence"]
        assert {row["location"]["page"] for row in rows} == {1, 2}
        assert "backup" in rows[0]["text"] and "Only then" in rows[1]["text"]
        return "Finish the backup before migrating; timeout is 42 seconds (physical page 2)."

    model_service.chat_response = original_source_answer(inspect)
    for operation in (ask_question, continue_conversation):
        assert (
            asyncio.run(operation(kb_dir, "Explain the migration prerequisite.")).status
            == "completed"
        )
