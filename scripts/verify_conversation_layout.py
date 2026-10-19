"""Native responsive chat, scrolling, drafts and composition acceptance."""

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
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QImage, QInputMethodEvent
    from PySide6.QtTest import QSignalSpy, QTest
    from PySide6.QtWidgets import QApplication, QLabel

    from ultrakb import config
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop.reader import MarkdownView
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_data import publish_pages, retain_conversation
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import kb_ingest_lock

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    kb = root / "对话版式库"
    initialize_kb(kb, seed_environment=False)
    app = create_application(settings_dir=root / "qt-settings")
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() not in {"offscreen", "minimal"}
    image = QImage(2000, 400, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    with kb_ingest_lock(kb / ".ultrakb"):
        image.save(str(kb / "wiki/wide.png"))
    publish_pages(kb, {"wide.png": (kb / "wiki/wide.png").read_bytes()})
    window = Workbench(history_dir=root / "tasks")
    window.resize(1366, 768)
    window.show()
    checks, widths = [], []

    def wait(condition, timeout=40):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError("Conversation acceptance timed out")
        app.processEvents()

    prose = "普通正文必须随工作区换行，宽代码不应拉伸正文。" * 10
    answer = "# 完整回答\n\n" + prose + "\n\n" + "这是可阅读的正文，窗口变化后仍然保留。\n\n" * 30
    answer += "| " + " | ".join(f"列{i}" for i in range(25)) + " |\n"
    answer += "| " + " | ".join("---" for _ in range(25)) + " |\n"
    answer += "| " + " | ".join("宽表格内容" for _ in range(25)) + " |\n\n"
    answer += (
        "```python\n"
        + "very_long_code_identifier_" * 50
        + "\n```\n\n![宽图](wide.png)\n\n$$a^2+b^2=c^2$$"
    )
    session_id, _ = retain_conversation(kb, [("请说明对话布局", answer)])
    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        button(window, "对话").click()
        window.conversations.open(session_id)
        wait(lambda: answer.splitlines()[0][2:] in window.chat.toPlainText())
        reader = next(entry[4] for entry in window.chat._entries if entry[4])
        wait(
            lambda: reader.rendering_stopped()
            or not reader._futures
            or all(f.done() for f in reader._futures)
        )
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((960, 680), (1366, 768), (1920, 1080)):
                window.showNormal()
                available = app.primaryScreen().availableGeometry()
                target_width = width
                if not args.headless:
                    target_width = min(width, available.width() - (240 if width == 960 else 40))
                window.resize(
                    target_width, height if args.headless else min(height, available.height() - 40)
                )
                deadline = time.monotonic() + 0.25
                while time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                wait(lambda: not reader._typing.isActive())
                left = reader.viewport().mapTo(window, QPoint(0, 0)).x()
                input_left = window.question.viewport().mapTo(window, QPoint(0, 0)).x()
                assert abs(left - input_left) < 15, (left, input_left)
                assert window.chat.horizontalScrollBar().maximum() == 0
                assert reader.document().rootFrame().frameFormat().leftMargin() <= 4
                widths.append(
                    {
                        "theme": theme,
                        "window": width,
                        "actual_window": window.width(),
                        "reader": reader.width(),
                        "input": window.question.width(),
                    }
                )
                assert not any(
                    label.isVisible()
                    and ("Enter 发送" in label.text() or "本对话 tokens" in label.text())
                    for label in window.findChildren(QLabel)
                )
                window.chat.verticalScrollBar().setValue(0)
                window.grab().save(str(root / f"{theme}-{width}.png"))
                assert reader.horizontalScrollBar().maximum() > 0
                assert reader.document().find(prose).block().layout().lineCount() > 1
                window.chat.verticalScrollBar().setValue(window.chat.verticalScrollBar().maximum())
                app.processEvents()
                window.grab().save(str(root / f"wide-content-{theme}-{width}.png"))
        (root / "geometry.json").write_text(json.dumps(widths, ensure_ascii=False, indent=2))
        assert max(row["reader"] for row in widths) > min(row["reader"] for row in widths) + 100
        standard = MarkdownView()
        standard.resize(1800, 400)
        standard.show()
        standard._reader_chrome()
        assert standard.document().rootFrame().frameFormat().leftMargin() > 300
        standard.stop_rendering()
        standard.close()
        checks.append(
            "A19/A20: fluid answer/input alignment, local wide-content scroll; "
            "normal readers retain width; two themes/three requested sizes"
        )
        bar = window.chat.verticalScrollBar()
        bar.setValue(min(80, bar.maximum()))
        before = bar.value()
        chat = window.conversations.active
        chat.question, chat.answer, chat.running = "后续问题", "正在流式生成的答案。" * 30, True
        window.conversations.render(restore_draft=False)
        wait(lambda: window.chat._entries[-1][4]._shown == chat.answer)
        assert abs(bar.value() - before) < 10, (bar.value(), before)
        assert window.conversation_notice.parent() is window.chat.canvas
        chat.running = False
        chat.failure = "回答已停止。问题已保留，可以继续提问。"
        chat.answer = ""
        window.conversations.render(restore_draft=False)
        assert "正在流式生成" not in window.chat.toPlainText()
        assert "完整回答" in window.chat.toPlainText()
        checks.append(
            "A24: reading above bottom stays anchored; stopped draft answer excluded; "
            "completed history retained"
        )
        question = window.question
        question.submitted.disconnect(window._ask)
        spy = QSignalSpy(question.submitted)
        question.setPlainText("中文草稿")
        question.setFocus()
        QApplication.sendEvent(question, QInputMethodEvent("nihao", []))
        QTest.keyClick(question, Qt.Key.Key_Return)
        assert spy.count() == 0
        commit = QInputMethodEvent()
        commit.setCommitString("你好")
        QApplication.sendEvent(question, commit)
        QTest.keyClick(question, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        assert spy.count() == 0 and "\n" in question.toPlainText()
        QTest.keyClick(question, Qt.Key.Key_Return)
        assert spy.count() == 1
        question.submitted.connect(window._ask)
        question.setPlainText("切换后保留的中文草稿")
        window.conversations.new()
        window.conversations.open(session_id)
        wait(lambda: not window.conversations.active.loading)
        assert question.toPlainText() == "切换后保留的中文草稿"
        checks.append(
            "A24: injected Qt composition commit does not send; "
            "Shift+Enter/newline and Enter/send; session drafts restored"
        )
        report = {
            "platform": app.platformName(),
            "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
            "checks": checks,
            "widths": widths,
            "model_sends": 0,
            "input_evidence": "QInputMethodEvent injected; actual OS IME separately tested",
            "fixture": "local stored display sample, no generated answer claim",
        }
        (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    finally:
        window.request_quit()
        wait(window.shutdown_complete)


if __name__ == "__main__":
    main()
