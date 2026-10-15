"""Actual Qt first import/answer loop; reviews reuse retained real results without sends."""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        os.environ.pop(key, None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from PySide6.QtCore import QMimeData, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QFileDialog, QLineEdit, QPushButton

    from ultrakb import config
    from ultrakb.application.conversations import read_conversation
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop.answer_actions import AnswerActions
    from ultrakb.desktop.reader import MarkdownView
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.runtime.records import TERMINAL

    original = args.review.resolve() if args.review else root
    config.GLOBAL_CONFIG_DIR = original / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    app = create_application()
    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    app.setQuitOnLastWindowClosed(False)
    assert app.platformName() == "xcb"
    clipboard = app.clipboard()
    mime = clipboard.mimeData()
    formats = {key: mime.data(key) for key in mime.formats()}
    window = Workbench(history_dir=original / "tasks")
    window.resize(1366, 768)
    window.show()
    checks, geometry = [], []
    live = None

    def wait(condition, timeout=50):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() >= deadline:
                raise TimeoutError("First-success acceptance timed out")
        app.processEvents()

    def keypress(widget, key=Qt.Key.Key_Space):
        widget.setFocus()
        QTest.keyClick(widget, key)
        app.processEvents()

    def enter_text(widget, text):
        clipboard.setText(text)
        widget.setFocus()
        QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(widget, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    def select_path(path):
        def choose():
            dialog = next(w for w in app.topLevelWidgets() if isinstance(w, QFileDialog))
            edit = dialog.findChild(QLineEdit, "fileNameEdit")
            assert edit is not None
            enter_text(edit, str(path))
            QTest.keyClick(edit, Qt.Key.Key_Return)

        QTimer.singleShot(100, choose)

    def visible_actions(identity):
        return next(
            (
                a
                for a in window.chat.findChildren(AnswerActions)
                if a.isVisible() and a.data.identity == identity
            ),
            None,
        )

    try:
        if args.review:
            prior = json.loads((original / "result.json").read_text())
            kb = Path(prior["kb"])
            window.open_knowledge_base(kb)
            wait(lambda: window.kb == kb and not window.io._callbacks)
            session_id = prior["session_id"]
            imported = json.loads((original / "import-task.json").read_text())
            window.workspaces.import_progress.track(kb, imported["id"])
            checks.append("Review uses a previously recorded genuine import and answer; zero sends")
        else:
            kb = root / "首次知识库"
            kb.mkdir()
            source = root / "审批规则.md"
            source.write_text(
                "# Approval rules\n\nApproval deadline: 7 days.\n"
                "Archive retention frequency: quarterly.\n"
            )
            select_path(kb)
            keypress(button(window, "新建知识库"))
            wait(lambda: window.kb == kb and not window.io._callbacks)
            assert not window.manager.tasks()
            window.grab().save(str(root / "empty-kb.png"))
            keypress(button(window, "资料"))
            from PySide6.QtWidgets import QToolButton

            importer = next(
                w
                for w in window.workspaces.import_controls.findChildren(QToolButton)
                if w.text() == "导入资料"
            )
            select_path(source)
            keypress(importer)
            wait(lambda: window.onboarding.dialog is not None)
            window.onboarding.dialog.reject()
            assert source.exists() and not window.manager.tasks()
            keypress(button(window, "概览"))
            keypress(window.workspaces.setup_continue)
            wait(lambda: window.onboarding.dialog is not None)
            dialog = window.onboarding.dialog
            editor = dialog.editors[0]
            wait(lambda: editor.form.isEnabled())
            from live_llm import LiveLLM
            from llm_test_budget import TestRunBudget

            live = LiveLLM(TestRunBudget(root / "cost.json"))
            credentials = live.credentials
            # The measured gateway owns real network sends; install its startup routing.
            os.environ["LLM_API_KEY"] = credentials.api_key
            os.environ["OPENAI_API_BASE"] = credentials.base_url
            editor.connection.service.setFocus()
            QTest.keyClick(editor.connection.service, Qt.Key.Key_Home)
            QTest.keyClick(editor.connection.service, Qt.Key.Key_Down)
            QTest.keyClick(editor.connection.service, Qt.Key.Key_Down)
            assert editor.fields["model"].text.text() == "deepseek/deepseek-flash"
            assert editor.fields["model_capacity"].text.text()
            assert not editor.advanced_toggle.isChecked()
            for key, value in (
                ("api_key", credentials.api_key),
                ("openai_api_base", credentials.base_url),
            ):
                editor.fields[key].action.setCurrentIndex(1)
                enter_text(editor.fields[key].text, value)
            dialog.grab().save(str(root / "connection-guide.png"))
            keypress(editor.connection.test_button)
            wait(lambda: window.onboarding.dialog is None and window.manager.tasks())
            progress = window.workspaces.import_progress
            wait(lambda: progress.task_id is not None)
            import_id = progress.task_id
            assert Path(window.manager.get(import_id).kb_dir) == kb
            keypress(button(window, "资料"))
            window.grab().save(str(root / "import-progress.png"))
            other = root / "另一个知识库"
            initialize_kb(other, seed_environment=False)
            window.open_knowledge_base(other)
            wait(lambda: window.kb == other)
            keypress(button(window, "知识"))
            wait(lambda: window.manager.get(import_id).state in TERMINAL, timeout=240)
            task = window.manager.get(import_id)
            assert task.state == "completed", task
            for _ in range(20):
                app.processEvents()
                time.sleep(0.01)
            assert window.kb == other and window.shell.title.text() == "知识"
            keypress(button(window, "资料"))
            wait(lambda: progress.ask.isEnabled())
            assert "当前正在浏览其他知识库" in progress.status.text()
            keypress(progress.ask)
            wait(lambda: window.kb == kb and window.question.isVisible())
            wait(lambda: window.conversations.active and not window.conversations.active.loading)
            assert source.exists()
            enter_text(
                window.question,
                "审批期限和归档保留频率分别是什么？请读取原文，给出精确值和可点击本库原文链接。",
            )
            QTest.keyClick(window.question, Qt.Key.Key_Return)
            wait(lambda: window.conversations.active.running)
            answer_id = window.conversations.active.task
            wait(lambda: window.manager.get(answer_id).state in TERMINAL, timeout=240)
            answer_task = window.manager.get(answer_id)
            assert answer_task.state == "completed", answer_task
            session_id = answer_task.results[-1].session_id
            wait(
                lambda: not window.conversations.active.running
                and not window.conversations.active.loading
            )
            checks.extend(
                [
                    "A01/A02/A18: keyboard create, import selector, cancel/resume guide, "
                    "service example, key entry and genuine probe; no advanced capacity editing",
                    "A14: genuine import stays in original KB; completion neither navigates nor "
                    "switches KB; explicit next step names and opens original target",
                ]
            )
            (root / "import-task.json").write_text(
                json.dumps(task.summary(), ensure_ascii=False, indent=2)
            )
            (root / "answer-task.json").write_text(
                json.dumps(answer_task.summary(), ensure_ascii=False, indent=2)
            )
        view = read_conversation(kb, session_id)
        answer = view.answers[-1]
        assert answer.citations and answer.citations["references"]
        assert any(ref["returned"] for ref in answer.citations["references"])
        (root / "answer.json").write_text(
            json.dumps(
                {
                    "identity": answer.identity,
                    "question": answer.question,
                    "markdown": answer.markdown,
                    "citations": answer.citations,
                    "usage": answer.usage,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        window.conversations.open(session_id)
        keypress(button(window, "对话"))
        wait(lambda: visible_actions(answer.identity))
        actions = visible_actions(answer.identity)
        from PySide6.QtWidgets import QPlainTextEdit

        def inspect_details():
            dialog = app.activeModalWidget()
            assert dialog is not None
            text = "\n".join(w.toPlainText() for w in dialog.findChildren(QPlainTextEdit))
            assert str(kb) in text
            dialog.grab().save(str(root / "import-task-details.png"))
            QTest.keyClick(dialog, Qt.Key.Key_Escape)

        keypress(button(window, "资料"))
        QTimer.singleShot(100, inspect_details)
        keypress(window.workspaces.import_progress.details)
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for size in ((960, 680), (1366, 768)):
                window.showNormal()
                screen = app.primaryScreen().availableGeometry()
                window.resize(
                    min(size[0], screen.width() - (240 if size[0] == 960 else 40)),
                    min(size[1], screen.height() - 40),
                )
                for page in ("资料", "设置", "对话"):
                    keypress(button(window, page))
                    wait(lambda: not window.io._callbacks)
                    if page == "资料":
                        panel = window.workspaces.panels["资料"][0]
                        wait(lambda: bool(panel.rows))
                        panel.table.setFocus()
                        QTest.keyClick(
                            panel.table, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier
                        )
                        assert panel.selection.checked
                        keypress(
                            next(
                                w
                                for w in panel.bulk_bar.findChildren(QPushButton)
                                if w.text() == "取消选择"
                            )
                        )
                        controls = panel.table.cellWidget(0, 3)
                        assert panel.table.rowHeight(0) >= controls.minimumSizeHint().height()
                        assert panel.table.columnWidth(3) >= controls.minimumSizeHint().width()
                    if page == "对话":
                        actions.sources.toggle.setChecked(True)
                        last = actions.sources.list.findChildren(QPushButton)[-1]
                        last.setFocus()
                        window.chat.ensureWidgetVisible(last)
                        assert window.chat.horizontalScrollBar().maximum() == 0
                    for _ in range(5):
                        app.processEvents()
                        time.sleep(0.01)
                    window.grab().save(str(root / f"{page}-{theme}-{size[0]}.png"))
                    geometry.append(
                        {
                            "page": page,
                            "theme": theme,
                            "requested": size,
                            "actual": [window.width(), window.height()],
                        }
                    )
        keypress(actions.copy)
        assert clipboard.text() == answer.markdown
        first = actions.sources.list.findChildren(QPushButton)[0]
        keypress(first)
        wait(lambda: hasattr(window, "_citation_dialog") and window._citation_dialog.isVisible())
        dialog = window._citation_dialog
        reader = dialog.findChildren(MarkdownView)[0]
        wait(lambda: "7 days" in reader.toPlainText() and "quarterly" in reader.toPlainText())
        dialog.grab().save(str(root / "verified-original.png"))
        keypress(next(w for w in dialog.findChildren(QPushButton) if w.text() == "返回回答"))
        checks.append(
            "A18/A21/A23: keyboard selection, ask, exact Markdown copy and captured "
            "original read-back; both stated values verified in the retained original"
        )
        report = {
            "platform": app.platformName(),
            "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
            "kb": str(kb),
            "session_id": session_id,
            "checks": checks,
            "geometry": geometry,
            "model_sends": 0 if args.review else None,
            "fixture": "real imported document and real model answer",
        }
        if live:
            report["cost"] = live.budget.report()
        (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        if hasattr(window, "_citation_dialog"):
            window._citation_dialog.accept()
        wait(lambda: all(task.state in TERMINAL for task in window.manager.tasks()))
        window.request_quit()
        wait(window.shutdown_complete)
        if live:
            live.close()
        if formats:
            restored = QMimeData()
            for key, value in formats.items():
                restored.setData(key, value)
            clipboard.setMimeData(restored)
        else:
            clipboard.clear()


if __name__ == "__main__":
    main()
