"""Conversation progress stays visible without showing model narration or reasoning."""

import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPlainTextEdit, QPushButton

from openkb.desktop.conversation_activity import ConversationActivity
from openkb.desktop.conversation_cards import ConversationView
from openkb.desktop.conversations import Chat, Conversations
from openkb.runtime.records import TaskView, UnitResult


@pytest.fixture
def chat_window(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = SimpleNamespace(
        manager=SimpleNamespace(history_dir=tmp_path / "tasks"),
        question=QPlainTextEdit(),
        ask_button=QPushButton(),
        stop_answer=QPushButton(),
        conversation_title=QLabel(),
        conversation_notice=ConversationActivity(),
        chat=ConversationView(),
        kb=tmp_path,
    )
    window.conversations = Conversations(window)
    chat = Chat(tmp_path, question="分区有什么要求？", task="a" * 32, running=True)
    window.conversations.active = chat
    window.conversations.tasks[chat.task] = chat
    yield window
    window.chat.stop_rendering()
    window.conversation_notice.setText("")
    app.processEvents()


def wait_until(predicate):
    end = time.monotonic() + 5
    while not predicate() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert predicate()


def test_review_step_is_visible_while_answer_is_not_finished(chat_window):
    task = TaskView(
        "a" * 32,
        str(chat_window.kb),
        "ContinueConversation",
        "running",
        "answer_review",
        1,
        (),
        False,
        False,
        text="<think>private reasoning</think>Unverified answer",
    )
    chat_window.conversations.observe(task)
    assert not chat_window.conversation_notice.isHidden()
    assert "核对" in chat_window.conversation_notice.text()
    assert "private reasoning" not in chat_window.chat.toPlainText()
    assert "Unverified answer" not in chat_window.chat.toPlainText()


def test_activity_continues_timing_without_model_text_and_stops_on_failure(chat_window):
    task = TaskView(
        "a" * 32,
        str(chat_window.kb),
        "ContinueConversation",
        "running",
        "answer_review",
        1,
        (),
        False,
        False,
        started_at=(datetime.now(timezone.utc) - timedelta(seconds=75)).isoformat(),
    )
    chat_window.conversations.observe(task)
    notice = chat_window.conversation_notice
    assert "1 分" in notice.text() and notice.timer.isActive()
    question_card = chat_window.chat.findChild(QFrame, "userMessage")
    chat_window.conversations.observe(replace(task, stage="answer_saving"))
    assert "保存" in notice.text()
    assert chat_window.chat.findChild(QFrame, "userMessage") is question_card
    chat_window.conversations.recover = lambda *args, **kwargs: None
    chat_window.conversations.finished(
        replace(
            task,
            state="failed",
            results=(
                UnitResult(
                    "failed",
                    error=(
                        "Conversation did not complete (answer_verification_invalid:quote_mismatch)"
                    ),
                ),
            ),
        )
    )
    assert "原文不一致" in notice.text()
    assert not notice.timer.isActive() and notice.bar.isHidden()
    assert "分区有什么要求" in chat_window.chat.toPlainText()


def test_rounded_cards_preserve_long_answers_links_and_history(chat_window):
    view = chat_window.chat
    view.resize(680, 420)
    view.show()
    answer = "<think>private reasoning</think>" + "\n\n".join(
        f"第 {i} 项分区要求。 [原文](sources/a.md#disk)" for i in range(25)
    )
    view.show_turns([("分区有什么要求？", answer)], chat_window.kb)
    wait_until(lambda: "第 24 项" in view.toPlainText())
    assert "private reasoning" not in view.toPlainText()
    reader = view._entries[-1][-1]
    assert reader.height() >= reader.document().size().height()
    wait_until(lambda: view.verticalScrollBar().maximum() > 0)
    assert "border-radius: 18px" in view.canvas.styleSheet()
    seen = []
    view.anchorClicked.connect(lambda url: seen.append(url.toString()))
    from PySide6.QtCore import QUrl

    reader.anchorClicked.emit(QUrl("sources/a.md#disk"))
    assert seen == ["sources/a.md#disk"]
    view.show_turns([("分区有什么要求？", answer)], chat_window.kb, pending=("继续", ""))
    assert view._entries[1][-1] is reader
    view.resize(360, 420)
    QApplication.processEvents()
    assert reader.height() >= reader.document().size().height()
    view.show_temporary("已切换对话")
    wait_until(view.rendering_stopped)
    assert view.toPlainText() == "已切换对话"
