"""Source focus, actual evidence snapshots and native reference navigation."""

import argparse
import faulthandler
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    faulthandler.dump_traceback_later(90, repeat=True)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from focus_fixtures import partial_workbook, unprocessed_source
    from publication_fixtures import publish_document
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QPushButton
    from test_answer_citations import read_originals

    from ultrakb import config
    from ultrakb.agent.chat_session import ChatSession
    from ultrakb.application.answer_citations import capture_answer_citations
    from ultrakb.application.conversations import read_conversation
    from ultrakb.application.document_actions import DocumentIdentity
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.model_connections import verify_model_connection
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.desktop.answer_actions import AnswerActions
    from ultrakb.desktop.document_rows import row_command
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.runtime.records import TERMINAL
    from ultrakb.source_catalog import read_source

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    live = None
    credentials = LlmCredentialBundle(api_key="unused-fixture")
    if args.real:
        from live_llm import LiveLLM
        from llm_test_budget import TestRunBudget

        from ultrakb.application.settings import apply_global_config_patch
        from ultrakb.application.settings_data import GlobalConfigPatchRequest

        live = LiveLLM(TestRunBudget(root / "cost.json"))
        credentials = live.credentials
        os.environ["LLM_API_KEY"] = credentials.api_key
        os.environ["OPENAI_API_BASE"] = credentials.base_url
        apply_global_config_patch(
            GlobalConfigPatchRequest(
                config={
                    "model": "deepseek/deepseek-flash",
                    "model_capacity": {"max_input_tokens": 32000, "tokenizer_model": "gpt-4o"},
                }
            )
        )
    kb = root / "关注资料库"
    other = root / "另一个库"
    initialize_kb(kb, seed_environment=False)
    initialize_kb(other, seed_environment=False)
    published = []
    for name, body in (
        ("评审", "Review period is seven days."),
        ("归档", "Archive retention is quarterly."),
    ):
        path = kb / "raw" / f"{name}.md"
        with kb_ingest_lock(kb / ".ultrakb"):
            atomic_write_text(path, f"# {name}\n\n{body}")
        published.append(publish_document(kb, path, body, bundle=credentials))
    partial = partial_workbook(kb, kb / "raw/部分工作簿.xlsx", bundle=credentials)
    missing = unprocessed_source(kb, kb / "raw/未处理.md")
    names = [read_source(kb, item.source_id).doc_name for item in published]
    selection = resolve_query_views(kb)
    actual_reader = read_originals(kb, selection, names)
    sample = (
        f"# 有据内容\n\nReview is seven days. [[sources/{names[0]}.content.json]]\n\n"
        f"Archive is quarterly. [[sources/{names[1]}.content.json]]\n\n"
        f"Again [[sources/{names[1]}.content.json]]"
    )
    sample_session = ChatSession.new(kb, "stored-ui-sample", "zh")
    sample_session.record_turn(
        "核对评审和归档",
        sample,
        [],
        citations=capture_answer_citations(sample, selection, actual_reader.returned_reads),
    )
    legacy = ChatSession.new(kb, "stored-ui-sample", "zh")
    legacy.record_turn("旧回答", "[[summaries/评审]]", [])
    if live:
        # Compiler fixtures can import CLI dotenv; recapture the measured gateway.
        os.environ["LLM_API_KEY"] = credentials.api_key
        os.environ["OPENAI_API_BASE"] = credentials.base_url
        assert verify_model_connection(kb).state == "passed"
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() == "xcb"
    window = Workbench(history_dir=root / "tasks")
    window.show()
    checks = []
    geometries = []

    def wait(condition, timeout=50):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError("Answer-source acceptance timed out")
        app.processEvents()

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        button(window, "资料").click()
        wait(lambda: "资料" in window.workspaces.panels)
        panel = window.workspaces.panels["资料"][0]
        wait(lambda: len(panel.rows) == 4)
        assert panel.rows[DocumentIdentity("source", partial)].actions["ask"].allowed
        assert not panel.rows[DocumentIdentity("source", missing)].actions["ask"].allowed
        row_command(panel, DocumentIdentity("source", missing), "ask")
        assert "更多" in panel.status.text()
        row_command(panel, DocumentIdentity("source", partial), "ask")
        wait(
            lambda: window.shell.current_page == "对话"
            if hasattr(window.shell, "current_page")
            else window.question.isVisible()
        )
        wait(
            lambda: "部分工作簿" in window.focus_caption.text()
            and "仍可使用" in window.focus_caption.text()
        )
        assert "部分完成" in window.focus_caption.text()
        checks.append(
            "A15: partial Alpha usable, Beta failed/Gamma pending; "
            "unprocessed ask disabled with recovery"
        )
        window.conversations.new()
        row_command(panel, DocumentIdentity("source", published[0].source_id), "ask")
        wait(
            lambda: window.conversations.active.focus
            == (DocumentIdentity("source", published[0].source_id),)
        )
        assert "当前知识库" in window.focus_caption.text()
        checks.append(
            "A16: row focus captured by Source ID, header explicitly full KB plus preference"
        )
        if live:
            window.question.setPlainText(
                "评审期限和归档保留频率分别是什么？请核对两个来源原文，分别给出精确值及可点击本库原文链接。"
            )
            window.ask_button.click()
            wait(
                lambda: any(
                    task.operation == "ContinueConversation" for task in window.manager.tasks()
                )
            )
            task_id = next(
                task.id
                for task in reversed(window.manager.tasks())
                if task.operation == "ContinueConversation"
            )
            wait(lambda: window.manager.get(task_id).state in TERMINAL, timeout=180)
            task = window.manager.get(task_id)
            assert task.state == "completed", task
            wait(
                lambda: not window.conversations.active.running
                and not window.conversations.active.loading
            )
            expected = read_conversation(kb, task.results[-1].session_id).answers[-1].identity
            wait(
                lambda: any(
                    action.isVisible() and action.data.identity == expected
                    for action in window.chat.findChildren(AnswerActions)
                )
            )
            actions = next(
                action
                for action in window.chat.findChildren(AnswerActions)
                if action.isVisible() and action.data.identity == expected
            )
            identities = {group.identity for group in actions.sources.groups}
            assert {item.source_id for item in published}.issubset(identities), identities
            assert all(
                any(reference["returned"] for reference in group.references)
                for group in actions.sources.groups
                if group.identity in {item.source_id for item in published}
            )
            assert live.budget.report()["provider_requests"] > 0
            (root / "task-result.json").write_text(
                json.dumps(task.summary(), ensure_ascii=False, indent=2)
            )
            checks.append(
                "Real SDK: focus review source; answer actually cites/read archive source too; "
                "full-KB retrieval preserved"
            )
        else:
            window.conversations.open(sample_session.id)
            wait(
                lambda: any(
                    action.isVisible() and action.data.identity == sample_session.turns[0].turn_id
                    for action in window.chat.findChildren(AnswerActions)
                )
            )
            actions = next(
                action
                for action in window.chat.findChildren(AnswerActions)
                if action.isVisible() and action.data.identity == sample_session.turns[0].turn_id
            )
        assert len(actions.sources.groups) >= 2
        wait(lambda: "正在核对" not in window.focus_caption.text())
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((960, 680), (1366, 768)):
                window.showNormal()
                screen = app.primaryScreen().availableGeometry()
                window.resize(
                    width
                    if args.headless
                    else min(width, screen.width() - (240 if width == 960 else 40)),
                    height if args.headless else min(height, screen.height() - 40),
                )
                actions.sources.toggle.setChecked(True)
                buttons = actions.sources.list.findChildren(QPushButton)
                buttons[-1].setFocus()
                window.chat.ensureWidgetVisible(buttons[-1])
                wait(lambda: actions.sources.height() > 100)
                for _ in range(5):
                    app.processEvents()
                    time.sleep(0.01)
                window.chat.ensureWidgetVisible(buttons[-1])
                app.processEvents()
                assert window.chat.horizontalScrollBar().maximum() == 0
                geometries.append(
                    {
                        "theme": theme,
                        "requested": [width, height],
                        "actual": [window.width(), window.height()],
                        "transcript": [window.chat.width(), window.chat.height()],
                    }
                )
                window.grab().save(str(root / f"sources-{theme}-{width}.png"))
        assert actions.sources.toggle.text() == f"出处（{len(actions.sources.groups)}）"
        before = window.chat.verticalScrollBar().value()
        first = actions.sources.list.findChildren(QPushButton)[0]
        first.setFocus()
        QTest.keyClick(first, Qt.Key.Key_Space)
        wait(lambda: hasattr(window, "_citation_dialog") and window._citation_dialog.isVisible())
        dialog = window._citation_dialog
        wait(
            lambda: dialog.findChildren(
                __import__("ultrakb.desktop.reader", fromlist=["MarkdownView"]).MarkdownView
            )[0]
            .toPlainText()
            .strip()
        )
        assert "生成时原文版本" in "\n".join(label.text() for label in dialog.findChildren(QLabel))
        dialog.grab().save(str(root / "original-evidence.png"))
        next(
            button for button in dialog.findChildren(QPushButton) if button.text() == "返回回答"
        ).click()
        app.processEvents()
        assert abs(window.chat.verticalScrollBar().value() - before) < 10
        # The body link invokes the same immutable reference, not current page navigation.
        reference = actions.sources.groups[0].references[0]
        actions.reader.anchorClicked.emit(QUrl("ultrakb:" + reference["target"]))
        wait(lambda: window._citation_dialog.isVisible())
        window._citation_dialog.accept()
        checks.append(
            "A23: stable source dedup/count; keyboard/footer and body links open captured original;"
            " return scroll preserved"
        )
        window.conversations.open(legacy.id)
        wait(
            lambda: any(
                action.isVisible() and action.data.identity == legacy.turns[0].turn_id
                for action in window.chat.findChildren(AnswerActions)
            )
        )
        old = next(
            action
            for action in window.chat.findChildren(AnswerActions)
            if action.isVisible() and action.data.identity == legacy.turns[0].turn_id
        )
        old.sources.toggle.click()
        assert old.sources.groups[0].historical_unknown
        assert "缺少证据快照" in "\n".join(
            label.text() for label in old.sources.findChildren(QLabel)
        )
        checks.append(
            "A23: old answer information gap explicit; "
            "no tool-log or focus-derived citation filling"
        )
        window.open_knowledge_base(other)
        wait(
            lambda: window.kb == other
            and window.conversations.active
            and window.conversations.active.root == other
        )
        assert not window.conversations.active.focus
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and window.conversations.active.root == kb)
        checks.append(
            "A16/A23: KB switch owns correct chat/focus; no submission sent to another KB"
        )
        report = {
            "platform": app.platformName(),
            "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
            "checks": checks,
            "geometry": geometries,
            "fixture": (
                "actual retained domain state/reader results; "
                "local sample answer is not model generated"
            ),
            "service": "official DeepSeek question" if live else "no model calls",
        }
        if live:
            report["cost"] = live.budget.report()
        (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        if hasattr(window, "_citation_dialog"):
            window._citation_dialog.accept()
        wait(lambda: all(task.state in TERMINAL for task in window.manager.tasks()))
        print("Shutting down reference acceptance", flush=True)
        window.request_quit()
        print("Waiting for background work", flush=True)
        wait(window.shutdown_complete)
        print("Background work stopped", flush=True)
        if live:
            live.close()
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
