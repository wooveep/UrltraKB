"""Real zero-send worker receipts drive next-step ownership and stale-click rejection."""

import time

import pytest
from publication_fixtures import publish_document


@pytest.mark.native
def test_import_progress_keeps_target_and_rechecks_a_delayed_next_step(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")

    from ultrakb import config
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.config import LlmCredentialBundle, save_config
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.runtime.records import TERMINAL
    from ultrakb.runtime.requests import ImportFile

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    roots = [tmp_path / "original", tmp_path / "other"]
    for kb in roots:
        initialize_kb(kb, seed_environment=False)
    kb, other = roots
    source = kb / "raw/existing.md"
    with kb_ingest_lock(kb / ".ultrakb"):
        atomic_write_text(source, "# Available original\n\nOriginal body.")
    published = publish_document(
        kb, source, "Original body.", bundle=LlmCredentialBundle(api_key="unused")
    )
    save_config(
        kb / ".ultrakb/config.yaml",
        {"model": "deepseek/deepseek-flash", "model_policy": {"max_calls": 0, "retries": 0}},
    )
    new_source = tmp_path / "unsent.md"
    new_source.write_text("# This model operation is deliberately not admitted.\n")
    app = create_application(settings_dir=tmp_path / "qt-settings")
    app.setQuitOnLastWindowClosed(False)
    window = Workbench(history_dir=tmp_path / "tasks")
    window.manager.require_verified_connection = False
    window.show()

    def wait(condition):
        deadline = time.monotonic() + 45
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            assert time.monotonic() < deadline
        app.processEvents()

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        # Desktop gate deliberately bypassed only for this zero-call worker fixture.
        task_id = window.manager.submit(
            kb, [ImportFile(str(new_source))], require_verified_connection=False
        )
        progress = window.workspaces.import_progress
        progress.track(kb, task_id)
        window.open_knowledge_base(other)
        wait(lambda: window.kb == other)
        window.shell.navigate("知识")
        wait(lambda: window.manager.get(task_id).state in TERMINAL)
        wait(lambda: progress.ask.isEnabled())
        assert window.kb == other and window.shell.title.text() == "知识"
        assert str(kb) in progress.status.text() and "其他知识库" in progress.status.text()
        before = len(window.manager.tasks())
        progress.ask.click()
        wait(lambda: window.kb == kb and window.question.isVisible())
        assert len(window.manager.tasks()) == before and new_source.exists()
        # A newer KB switch while availability waits revokes that pending navigation.
        with kb_ingest_lock(kb / ".ultrakb"):
            progress.begin_question()
            window.open_knowledge_base(other)
        wait(lambda: window.kb == other and not window.io._callbacks)
        for _ in range(20):
            app.processEvents()
            time.sleep(0.01)
        assert window.kb == other
        assert not list((kb / ".ultrakb/usage/requests").glob("*.json"))
        from ultrakb.application.document_actions import DocumentIdentity
        from ultrakb.application.removal import preview_removal
        from ultrakb.runtime.requests import RemoveDocument

        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        window.conversations.active.focus = (DocumentIdentity("source", published.source_id),)
        window.conversations.render()
        wait(lambda: "existing.md" in window.focus_caption.text())
        preview = preview_removal(kb, published.source_id)
        assert preview.status == "ready" and preview.version
        removed = window.manager.submit(kb, [RemoveDocument(published.source_id, preview.version)])
        wait(lambda: window.manager.get(removed).state in TERMINAL)
        assert window.manager.get(removed).state == "completed"
        wait(lambda: progress._available is True)
        assert "existing.md" in window.focus_caption.text()
        progress.poll()
        wait(lambda: progress.ask.isEnabled())
        assert (
            "Original body."
            in window.read_scope.knowledge.read(
                f"summaries/{published.units[0].doc_name}.md"
            ).decode()
        )
    finally:
        wait(lambda: all(t.state in TERMINAL for t in window.manager.tasks()))
        window.request_quit()
        wait(window.shutdown_complete)
