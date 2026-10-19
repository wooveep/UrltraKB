"""Fresh KB setup, real document import and questions share usable capacity and readers."""

import asyncio
from time import monotonic

import pytest
from error_transport import ErrorTransport
from native_catalog_fixtures import original_bytes


@pytest.fixture
def room_document(tmp_path):
    import pymupdf

    path = tmp_path / "rooms.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text(
            (72, 72),
            "The indoor room permits quiet reading.\nThe garden room is closed.",
        )
        pdf.save(path)
    return path


def wait_until(app, condition, timeout=25):
    from PySide6.QtTest import QTest

    deadline = monotonic() + timeout
    while not condition():
        app.processEvents()
        assert monotonic() < deadline, "The actual setup I/O did not complete"
        QTest.qWait(10)
    app.processEvents()


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["import", "question"])
def test_fresh_kb_blocks_model_work_without_capacity(
    tmp_path, monkeypatch, room_document, operation
):
    from ultrakb import config
    from ultrakb.application.knowledge_bases import get_kb_list, initialize_kb
    from ultrakb.application.model_connections import read_connection_state
    from ultrakb.runtime.requests import ContinueConversation, ImportFile
    from ultrakb.runtime.tasks import TaskManager

    for key in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    kb = tmp_path / "kb"
    original = room_document.read_bytes()
    with ErrorTransport(status=401) as service:
        initialize_kb(
            kb,
            model="deepseek/deepseek-flash",
            api_key="offline-capacity-key",
            openai_api_base=service.url,
            seed_environment=False,
        )
        assert config.load_config(kb / ".ultrakb/config.yaml").get("model_capacity") is None
        state = read_connection_state(kb)
        assert state.state == "unverified"
        request = (
            ImportFile(str(room_document))
            if operation == "import"
            else ContinueConversation("Which room permits quiet reading?")
        )
        manager = TaskManager(history_dir=tmp_path / "tasks", max_workers=1)
        try:
            task_id = manager.submit(kb, [request], require_verified_connection=True)
            task = manager.wait(task_id, timeout=20)
            assert task.state == "blocked" and task.processes_reaped
            assert task.succeeded == 0 and task.results[0].turn_count == 0
            assert task.results[0].unfinished == ("connection_setup",)
            assert task.model_usage is None
            from native_usage_fixtures import read_requests

            assert read_requests(kb) == []
            assert service.requests == []
            assert get_kb_list(kb)["documents"] == []
            assert list((kb / ".ultrakb/chats").glob("*.json")) == []
            assert room_document.read_bytes() == original
        finally:
            manager.shutdown(stop=True)
            assert manager.join(15)


@pytest.mark.integration
@pytest.mark.parametrize("existing_limit", [None, 60000])
def test_retesting_unchanged_deepseek_prepares_capacity_for_the_actual_question(
    tmp_path, monkeypatch, existing_limit
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    for key in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    from PySide6.QtWidgets import QApplication

    from ultrakb import config
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.application.conversations import ask_question
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.config_state import capture_config
    from ultrakb.desktop.io import LocalIO
    from ultrakb.desktop.setup_connection import ConnectionSetup
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.model_tokenizer import question_measure
    from ultrakb.mutation import mutation_scope

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    app = QApplication.instance() or QApplication([])
    kb = tmp_path / "kb"
    with ErrorTransport(status=401) as service:
        initialize_kb(
            kb,
            model="deepseek/deepseek-flash",
            api_key="offline-capacity-key",
            openai_api_base=service.url,
            seed_environment=False,
        )
        settings = config.load_config(kb / ".ultrakb/config.yaml")
        assert settings.get("model_capacity") is None
        if existing_limit is not None:
            settings["model_capacity"] = {
                **template_capacity(CONNECTION_TEMPLATES[2]),
                "max_input_tokens": existing_limit,
            }
        settings.update(timeout=1, language="en")
        settings["model_policy"] = {"max_calls": 4, "retries": 0, "backoff": 0}
        settings["role_generation"] = {"answer": {"max_tokens": 32, "reasoning_effort": "none"}}
        config.save_config(kb / ".ultrakb/config.yaml", settings)
        source_path = kb / "wiki/sources/original.md"
        with (
            kb_ingest_lock(kb / ".ultrakb"),
            mutation_scope(kb, [source_path], operation="verify-question-original"),
        ):
            atomic_write_text(source_path, "The retained original states 42.")
        from ultrakb.desktop.verification_data import retain_document

        retain_document(kb, "original.md", "original", "The retained original states 42.")
        assert resolve_query_views(kb).has_evidence
        io = LocalIO()
        panel = ConnectionSetup(io, kb)
        try:
            wait_until(app, lambda: panel.view is not None and not panel.busy)
            # Re-select the supported service but retain the saved model and
            # address. The loopback endpoint only supplies genuine HTTP errors.
            panel.service.setCurrentIndex(2)
            panel.base_url.setText(service.url)
            panel.set_step(2)
            panel.verify()
            wait_until(app, lambda: not panel.busy and panel.state is not None)
            assert panel.state.code == "authentication"
            assert len(service.requests) == 1
            if existing_limit is None:
                # A private endpoint cannot acquire official limits by model name.
                from ultrakb.model_tokenizer import ContextCapacityError

                assert panel.view.values.model_capacity is None
                with kb_ingest_lock(kb / ".ultrakb"):
                    with pytest.raises(ContextCapacityError):
                        question_measure(capture_config(kb))
                assert len(service.requests) == 1
                return
            assert panel.view.values.model_capacity.max_input_tokens == existing_limit
            with kb_ingest_lock(kb / ".ultrakb"):
                measure = question_measure(capture_config(kb))
            measure.require_current("Read the original measurement.")
            result = asyncio.run(ask_question(kb, "Read the original measurement."))
            assert result.status == "failed" and "AuthenticationError" in result.error
            assert len(service.requests) == 2
        finally:
            panel.close_setup()
            io.stop()
            wait_until(app, io.stopped)
            panel.close()


@pytest.mark.live
def test_fresh_kb_imports_native_pages_and_answers_in_spawn_workers(
    tmp_path, monkeypatch, live_llm, room_document
):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from ultrakb import config
    from ultrakb.application.conversations import read_conversation
    from ultrakb.application.knowledge_bases import get_kb_list, initialize_kb
    from ultrakb.application.query_views import read_selected_file, resolve_query_views
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

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    live_llm.install(monkeypatch)
    app = QApplication.instance() or QApplication([])
    kb = tmp_path / "kb"
    initialize_kb(kb, model="deepseek/deepseek-flash", seed_environment=False)
    settings = config.load_config(kb / ".ultrakb/config.yaml")
    assert settings.get("model_capacity") is None
    settings.update(timeout=30, language="en")
    # This known test gateway forwards to the declared model service.
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES

    settings["model_capacity"] = template_capacity(CONNECTION_TEMPLATES[2])
    settings["model_policy"] = {"max_calls": 8, "retries": 0, "deadline_seconds": 90}
    settings["role_generation"] = {
        "compile": {"max_tokens": 1024, "reasoning_effort": "none"},
        "answer": {"max_tokens": 256, "reasoning_effort": "none"},
    }
    config.save_config(kb / ".ultrakb/config.yaml", settings)
    assert get_kb_list(kb)["documents"] == []
    assert list((kb / "wiki/sources").glob("*.json")) == []
    original = room_document.read_bytes()
    request_start = len(live_llm.requests)
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
        assert panel.view.values.model_capacity.applies_to_model == "deepseek/deepseek-flash"
        assert panel.view.values.model_capacity.context_window_tokens > 65536
        with kb_ingest_lock(kb / ".ultrakb"):
            question_measure(capture_config(kb)).require_current(
                "Which room permits quiet reading?"
            )
        manager = TaskManager(history_dir=tmp_path / "tasks", max_workers=1)
        import_id = manager.submit(
            kb,
            [ImportFile(str(room_document))],
            require_verified_connection=True,
        )
        wait_until(
            app,
            lambda: manager.get(import_id).state in TERMINAL
            and manager.get(import_id).processes_reaped,
            timeout=120,
        )
        imported = manager.get(import_id)
        assert imported.state == "completed", imported.error
        assert imported.succeeded == 1 and not imported.results[0].unfinished
        assert imported.results[0].import_units[0].knowledge_revision_id
        assert imported.model_usage["current"]["actual_sends"] > 0
        assert imported.model_usage["current"]["usage_complete"]
        document = get_kb_list(kb)["documents"][0]
        saved = read_document_source(kb, document["source_id"], pages="1")
        assert saved["unit_kind"] == "page" and saved["pages"] == 1
        assert "indoor room permits quiet reading" in saved["content"]
        assert original_bytes(kb, saved["source_revision_id"]) == original
        selected = resolve_query_views(kb)
        assert selected.has_evidence and len(selected.views) == 1
        view = selected.views[0]
        reader = source_capabilities(view)["rooms"]
        assert reader["unit_kind"] == "page"
        assert reader["reading_action"]["tool"] == "get_page_content"
        assert reader["legal_ranges"]["pages"] == "1-1"
        assert reader["source_revision_id"] == saved["source_revision_id"]
        source_bytes = read_selected_file(view, reader["path"])
        answer_request_start = len(live_llm.requests)
        task_id = manager.submit(
            kb,
            [
                ContinueConversation(
                    "Read page 1 of rooms using get_page_content, then state which room "
                    "permits quiet reading. Reply in one sentence."
                )
            ],
            require_verified_connection=True,
        )
        wait_until(
            app,
            lambda: manager.get(task_id).state in TERMINAL
            and manager.get(task_id).processes_reaped,
            timeout=120,
        )
        task = manager.get(task_id)
        assert task.state == "completed", task.error
        assert task.succeeded == 1 and task.unfinished == 0
        result = task.results[0]
        assert result.session_id and result.turn_count == 1
        session = read_conversation(kb, result.session_id)
        assert session.total_turns == 1
        answer = session.turns[0][1]
        assert answer == result.output and "indoor" in answer.lower()
        assert read_selected_file(view, reader["path"]) == source_bytes
        assert room_document.read_bytes() == original
        assert task.model_usage["current"]["actual_sends"] > 0
        assert task.model_usage["current"]["usage_complete"]
        answer_requests = live_llm.requests[answer_request_start:]
        assert any(
            tool["function"]["name"] == "get_page_content"
            for request in answer_requests
            for tool in request.get("tools", [])
        )
        assert any(request.get("stream") for request in answer_requests)
        assert any(
            call["function"]["name"] == "get_page_content"
            for request in answer_requests
            for message in request["messages"]
            for call in message.get("tool_calls", [])
        )
        assert any(
            message["role"] == "tool"
            and "Physical page 1" in str(message.get("content", ""))
            and "indoor room permits quiet reading" in str(message.get("content", ""))
            for request in answer_requests
            for message in request["messages"]
        )
        requests = live_llm.requests[request_start:]
        assert 4 <= len(requests) <= 18
        assert all(request["model"] == "deepseek-flash" for request in requests)
        assert live_llm.budget.exposure_cny <= 2
    finally:
        if manager is not None:
            manager.shutdown(stop=True)
            assert manager.join(15)
        panel.close_setup()
        io.stop()
        wait_until(app, io.stopped)
        panel.close()
