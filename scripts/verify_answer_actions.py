"""Actual Qt clipboard, per-answer statistics and unfinished-answer boundaries."""

import argparse
import json
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ["ULTRAKB_CONFIG_DIR"] = str(root / "settings")
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QMimeData, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel

    from ultrakb import config
    from ultrakb.agent.answer_text import visible_answer
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.views import published_scope
    from ultrakb.desktop.answer_actions import AnswerActions, character_count
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_data import publish_pages
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.storage.session_result import reference_trace
    from ultrakb.storage.session_store import SessionStore

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    kb = root / "回答操作库"
    initialize_kb(kb, seed_environment=False)
    with kb_ingest_lock(kb / ".ultrakb"):
        atomic_write_text(kb / "wiki/summaries/policy.md", "# Review policy\n\nSeven days.")
    publish_pages(kb, {"summaries/policy.md": (kb / "wiki/summaries/policy.md").read_bytes()})
    sessions = SessionStore(kb, published_scope(kb).knowledge.identity)
    session_id = "answer-actions-sample"
    sessions.create(session_id, model="stored-ui-sample", language="zh")
    source = "<think>private sample reasoning</think># 第一条回答\n\n"
    source += "**中文**，English! é 👨‍👩‍👧‍👦 🇨🇳\n\n"
    source += "正文随窗口换行。\n\n" * 20
    source += "```python\na + b\n```\n\n|列|值|\n|--|--|\n|中|2|\n\n[[summaries/policy]]\n\n$$x^2$$"
    first_usage = {"input_cached": 100, "input_uncached": 20, "output": 10, "reasoning": 2}
    sessions.append_exchange(
        session_id,
        "first-turn",
        "第一问",
        source,
        metadata={
            "schema": 1,
            "trace": reference_trace([], source),
            "result": {"usage": first_usage},
        },
    )
    sessions.append_exchange(session_id, "second-turn", "第二问", "第二条没有已有单条用量。")
    sessions.queue_submission(
        session_id, "unfinished", "未完成问题", view_id="legacy", after_turn=2
    )
    app = create_application(settings_dir=root / "qt-settings")
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() == "xcb"
    old_clipboard = QMimeData()
    original = app.clipboard().mimeData()
    if original:
        for format_ in original.formats():
            old_clipboard.setData(format_, original.data(format_))
    window = Workbench(history_dir=root / "tasks")
    window.show()
    checks, details = [], []

    def wait(condition, timeout=35):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError("Answer-action acceptance timed out")
        app.processEvents()

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        button(window, "对话").click()
        window.conversations.open(session_id)
        wait(lambda: len(window.chat.findChildren(AnswerActions)) == 2)
        bars = window.chat.findChildren(AnswerActions)
        first = next(bar for bar in bars if bar.data.identity == "first-turn")
        second = next(bar for bar in bars if bar.data.identity == "second-turn")
        assert all(bar.isVisible() for bar in bars)
        assert any(label.text() == "未完成提示" for label in window.chat.findChildren(QLabel))
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
                wait(
                    lambda: first.isVisible()
                    and second.isVisible()
                    and all(f.done() for f in first.reader._futures)
                    and all(f.done() for f in second.reader._futures)
                )
                window.chat.ensureWidgetVisible(first)
                app.processEvents()
                window.grab().save(str(root / f"{theme}-{width}.png"))
        window.chat.ensureWidgetVisible(first)
        first.copy.setFocus()
        QTest.keyClick(first.copy, Qt.Key.Key_Space)
        assert app.clipboard().text() == visible_answer(source)
        assert "已复制" == first.feedback.text() and "private sample" not in app.clipboard().text()
        second.copy.click()
        assert app.clipboard().text() == "第二条没有已有单条用量。"
        checks.append(
            "A21: real Qt clipboard via keyboard and mouse copies exact completed "
            "Markdown/citations, stable turn IDs"
        )

        class BrokenClipboard:
            def setText(self, value):
                raise OSError("Injected clipboard failure")

        original_factory = first.clipboard
        first.clipboard = lambda: BrokenClipboard()
        first.copy.click()
        assert first.feedback.text() == "复制失败，请重试"
        first.clipboard = original_factory
        checks.append("A21: injected clipboard fault reports failure; no false success")
        assert first.count == character_count(first.reader.document().toPlainText())
        assert "\ufffc" in first.reader.document().toPlainText()
        for target in (first, second):

            def collect():
                for dialog in QApplication.topLevelWidgets():
                    if dialog.windowTitle() == "本条回答统计" and dialog.isVisible():
                        details.append(
                            "\n".join(label.text() for label in dialog.findChildren(QLabel))
                        )
                        dialog.grab().save(str(root / f"statistics-{target.data.identity}.png"))
                        dialog.accept()

            QTimer.singleShot(80, collect)
            target.statistics.click()
        assert "输入命中缓存：100" in details[0] and "输出：10" in details[0]
        assert "9999" not in details[0] and "未提供单条模型用量" in details[1]
        checks.append(
            "A22: actual rendered cluster count; math graphic excluded; "
            "only single-turn usage, absent remains absent"
        )
        chat = window.conversations.active
        window.chat.verticalScrollBar().setValue(80)
        before = window.chat.verticalScrollBar().value()
        chat.question, chat.answer, chat.running = "流式问题", "未完成的流式正文。" * 10, True
        window.conversations.render(restore_draft=False)
        wait(lambda: window.chat._entries[-1][4]._shown == chat.answer)
        assert len(window.chat.findChildren(AnswerActions)) == 2
        assert window.chat._entries[-1][4].actions is None
        assert abs(window.chat.verticalScrollBar().value() - before) < 10
        checks.append(
            "A24: streaming and persisted unfinished notices have no completed-answer "
            "statistics; scroll stays anchored"
        )
        chat.running = False
        report = {
            "platform": app.platformName(),
            "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
            "checks": checks,
            "model_sends": 0,
            "character_count": first.count,
            "fixture": "locally recorded history/usage sample; not a generated response",
            "clipboard_fault": "injected; success uses actual Qt clipboard",
        }
        (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    finally:
        if old_clipboard.formats():
            app.clipboard().setMimeData(old_clipboard)
        else:
            app.clipboard().clear()
        window.request_quit()
        wait(window.shutdown_complete)


if __name__ == "__main__":
    main()
