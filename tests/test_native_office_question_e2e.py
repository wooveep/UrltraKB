"""Application end-to-end: fresh KB, real Office processing, publication and streamed QA."""

import json
import os
from pathlib import Path

import pytest
from native_question_inputs import write_office_input
from test_setup_question_capacity import wait_until

pytestmark = [pytest.mark.live, pytest.mark.native]


@pytest.fixture(scope="session")
def private_office_runtime():
    from ultrakb.office.runtime import validate_runtime

    configured = os.environ.get("ULTRAKB_TEST_OFFICE_RUNTIME")
    root = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parents[1] / "ultrakb/office/assets/runtime"
    ).resolve()
    if not (root / "ultrakb-office.json").is_file():
        pytest.skip("Prepare the private Office runtime or set ULTRAKB_TEST_OFFICE_RUNTIME")
    return root, validate_runtime(root)


@pytest.mark.parametrize("kind", ["docx", "pptx", "xlsx"])
def test_fresh_office_import_delivers_native_originals_to_a_real_answer(
    tmp_path, monkeypatch, live_llm, request, kind
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from ultrakb import config
    from ultrakb.agent.chat_session import load_session
    from ultrakb.application.knowledge_bases import get_kb_list, initialize_kb
    from ultrakb.application.query_views import read_selected_file, resolve_query_views
    from ultrakb.application.sources import source_inventory
    from ultrakb.config_state import capture_config
    from ultrakb.desktop.io import LocalIO
    from ultrakb.desktop.setup_connection import ConnectionSetup
    from ultrakb.documents import read_document_source
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.model_tokenizer import question_measure
    from ultrakb.runtime.records import TERMINAL
    from ultrakb.runtime.requests import ContinueConversation, ImportFile
    from ultrakb.runtime.tasks import TaskManager
    from ultrakb.source_reading import source_capabilities

    runtime = request.getfixturevalue("private_office_runtime") if kind != "xlsx" else None
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", tmp_path / "profile/.lock")
    live_llm.install(monkeypatch)
    app = QApplication.instance() or QApplication([])
    kb = tmp_path / "kb"
    initialize_kb(kb, model="deepseek/deepseek-flash", seed_environment=False)
    settings = config.load_config(kb / ".ultrakb/config.yaml")
    assert settings.get("model_capacity") is None and get_kb_list(kb)["documents"] == []
    settings.update(timeout=30, language="en")
    settings["model_policy"] = {
        # A workbook shares one execution across both sheets, including the
        # compiler's admission for any necessary repair requests.
        "max_calls": 20 if kind == "xlsx" else 12,
        "retries": 0,
        "deadline_seconds": 90,
    }
    settings["role_generation"] = {
        "compile": {"max_tokens": 1024, "reasoning_effort": "none"},
        "answer": {"max_tokens": 256, "reasoning_effort": "none"},
    }
    if runtime:
        settings["office_runtime_path"] = str(runtime[0])
    config.save_config(kb / ".ultrakb/config.yaml", settings)
    source = write_office_input(tmp_path / f"manual.{kind}")
    original = source.read_bytes()
    call_start = len(live_llm.requests)
    io, manager = LocalIO(), None
    panel = ConnectionSetup(io, kb)
    try:
        wait_until(app, lambda: panel.view is not None and not panel.busy)
        panel.service.setCurrentIndex(2)
        panel.base_url.setText(live_llm.credentials.base_url)
        panel.set_step(2)
        panel.verify()
        wait_until(app, lambda: not panel.busy and panel.state is not None)
        assert panel.state.state == "passed", panel.state.message
        capacity = panel.view.values.model_capacity
        assert capacity.applies_to_model == "deepseek/deepseek-flash"
        assert capacity.max_input_tokens == 65536
        with kb_ingest_lock(kb / ".ultrakb"):
            question_measure(capture_config(kb)).require_current("Read the imported original.")
        manager = TaskManager(history_dir=tmp_path / "tasks", max_workers=1)
        task_id = manager.submit(kb, [ImportFile(str(source))], require_verified_connection=True)
        wait_until(
            app,
            lambda: manager.get(task_id).state in TERMINAL
            and manager.get(task_id).processes_reaped,
            timeout=180,
        )
        imported = manager.get(task_id)
        assert imported.state == "completed", imported.error
        assert imported.succeeded == 1 and not imported.results[0].unfinished
        units = imported.results[0].import_units
        assert len(units) == (2 if kind == "xlsx" else 1)
        assert all(unit.status == "completed" and unit.knowledge_revision_id for unit in units)
        assert imported.model_usage["current"]["actual_sends"] > 0
        assert imported.model_usage["current"]["usage_complete"]
        inventories = source_inventory(kb)
        assert len(inventories) == 1
        source_id = inventories[0]["source_id"]
        selection = resolve_query_views(kb)
        assert selection.has_evidence and len(selection.views) == 1
        view = selection.views[0]
        readers = source_capabilities(view)
        assert len(readers) == len(units)
        source_bytes = {
            value["path"]: read_selected_file(view, value["path"]) for value in readers.values()
        }

        if kind == "xlsx":
            assert {unit.name for unit in units} == {"Public", "Private Budget"}
            target = next(unit for unit in units if unit.name == "Private Budget")
            public = next(unit for unit in units if unit.name == "Public")
            saved = read_document_source(kb, source_id, unit_id=target.unit_id, cells="B2")
            other = read_document_source(kb, source_id, unit_id=public.unit_id, cells="B2")
            assert saved["sheet"]["name"] == "Private Budget"
            assert saved["sheet"]["state"] == "hidden"
            assert "permitted" in saved["content"] and "forbidden" not in saved["content"]
            assert "forbidden" in other["content"] and "permitted" not in other["content"]
            cells = [origin["sheet_cell"] for origin in saved["origin_locators"]]
            assert len(cells) == 1 and cells[0]["cell"]["coordinate"] == "B2"
            reader = readers[target.doc_name]
            assert reader["unit_kind"] == "worksheet" and "B2" in reader["legal_ranges"]["cells"]
            tool, answer_fact, excluded_fact = "get_cell_content", "permitted", "forbidden"
            question = (
                "Read index.md, locate worksheet Private Budget, then use get_cell_content "
                "to read only its B2 cell. Reply with the stored rule and worksheet name."
            )
        else:
            saved = read_document_source(kb, source_id, pages="1")
            assert saved["unit_kind"] == "page" and saved["pages"] == 1
            assert saved["office"]["runtime_fingerprint"] == runtime[1].fingerprint
            assert saved["office"]["build_id"] == runtime[1].build_id
            assert (kb / saved["internal_pdf_path"]).read_bytes().startswith(b"%PDF-")
            reader = readers[units[0].doc_name]
            if kind == "pptx":
                body = read_document_source(kb, source_id, pages="1", part="body")
                notes = read_document_source(kb, source_id, pages="1", part="notes")
                assert "BODY-17" in body["content"] and "NOTE-42" not in body["content"]
                assert "NOTE-42" in notes["content"] and "BODY-17" not in notes["content"]
                assert len(saved["office"]["slides"]) == 1
                assert reader["unit_kind"] == "slide" and reader["legal_ranges"]["slides"] == "1-1"
                tool, answer_fact, excluded_fact = "get_slide_content", "NOTE-42", "BODY-17"
                question = (
                    "Read slide 1 of manual using get_slide_content with part=notes. "
                    "Reply only with the speaker-note code."
                )
            else:
                assert "indoor room permits quiet reading" in saved["content"]
                assert reader["unit_kind"] == "page" and reader["legal_ranges"]["pages"] == "1-1"
                tool, answer_fact, excluded_fact = "get_page_content", "indoor", None
                question = (
                    "Read page 1 of manual using get_page_content, then state which room "
                    "permits quiet reading. Reply in one sentence."
                )
        assert reader["reading_action"]["tool"] == tool
        assert reader["source_revision_id"] == saved["source_revision_id"]
        assert (kb / saved["original_path"]).read_bytes() == original
        answer_start = len(live_llm.requests)
        answer_id = manager.submit(
            kb, [ContinueConversation(question)], require_verified_connection=True
        )
        wait_until(
            app,
            lambda: manager.get(answer_id).state in TERMINAL
            and manager.get(answer_id).processes_reaped,
            timeout=120,
        )
        task = manager.get(answer_id)
        assert task.state == "completed" and task.unfinished == 0, task.error
        result = task.results[0]
        session = load_session(kb, result.session_id)
        assert result.turn_count == session.turn_count == 1
        answer = session.turns[0].assistant_text
        assert answer == result.output and answer_fact.lower() in answer.lower()
        if excluded_fact is not None:
            assert excluded_fact.lower() not in answer.lower()
        if kind == "xlsx":
            assert "private budget" in answer.lower()
        requests = live_llm.requests[answer_start:]
        assert any(message.get("stream") for message in requests)
        expected = {
            "doc_name": reader["doc_name"],
            **(
                {"slides": "1", "part": "notes"}
                if kind == "pptx"
                else {"cells": "B2"}
                if kind == "xlsx"
                else {"pages": "1"}
            ),
        }
        matched_calls = set()
        for outbound in requests:
            for message in outbound["messages"]:
                for call in message.get("tool_calls", []):
                    if call["function"]["name"] == tool:
                        arguments = json.loads(call["function"]["arguments"])
                        if all(arguments.get(key) == value for key, value in expected.items()):
                            matched_calls.add(call["id"])
        assert matched_calls, "The same native call must select the target document and location"
        assert any(
            message["role"] == "tool"
            and message.get("tool_call_id") in matched_calls
            and "original_source" in str(message.get("content", ""))
            and answer_fact.lower() in str(message.get("content", "")).lower()
            for outbound in requests
            for message in outbound["messages"]
        )
        assert task.model_usage["current"]["actual_sends"] > 0
        assert task.model_usage["current"]["usage_complete"]
        assert all(read_selected_file(view, path) == data for path, data in source_bytes.items())
        assert source.read_bytes() == original
        outgoing = live_llm.requests[call_start:]
        assert 4 <= len(outgoing) <= 26
        assert all(value["model"] == "deepseek-flash" for value in outgoing)
        assert live_llm.budget.exposure_cny <= 2
    finally:
        if manager is not None:
            manager.shutdown(stop=True)
            assert manager.join(15)
        panel.close_setup()
        io.stop()
        wait_until(app, io.stopped)
        panel.close()
