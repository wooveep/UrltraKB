"""Native chat acceptance: visible history, automatic context and task isolation."""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox


def verify_conversations(window, kb, root, wait):
    from openkb.agent.chat_session import ChatSession
    from openkb.desktop.verification_workbench import button
    from openkb.locks import kb_ingest_lock
    from openkb.runtime.records import TERMINAL

    session = ChatSession.new(kb, "openai/verification", "zh")
    session.record_turn("第一轮：介绍知识库", "第一轮的完整回答。", [])
    session.record_turn(
        "第二轮：继续展开",
        "<think>private hidden reasoning</think>第二轮的完整回答。\n\n`<think>` 是代码示例。",
        [],
    )
    window._refresh()
    button(window, "对话").click()
    button(window, "对话历史").click()
    wait(lambda: "对话" in window.workspaces.panels)
    history = window.workspaces.panels["对话"][0]
    wait(
        lambda: any(
            history.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == session.id
            for index in range(history.table.rowCount())
        )
    )
    row = next(
        index
        for index in range(history.table.rowCount())
        if history.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == session.id
    )
    QTest.mouseClick(
        history.table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=history.table.visualItemRect(history.table.item(row, 0)).center(),
    )
    wait(lambda: "第二轮的完整回答" in window.chat.toPlainText())
    assert "第一轮的完整回答" in window.chat.toPlainText()
    assert "private hidden reasoning" not in window.chat.toPlainText()
    assert "<think>" in window.chat.toPlainText()
    assert not any(
        combo.isVisible() and combo.accessibleName() in {"问答模式", "对话会话"}
        for combo in window.findChildren(QComboBox)
    )
    assert window.conversations.active.identity == session.id
    window.question.setPlainText("第三轮：在原对话中继续")
    with kb_ingest_lock(kb / ".openkb"):
        before = len(window.manager.tasks())
        QTest.keyClick(window.question, Qt.Key.Key_Return)
        task = window.manager.tasks()[-1]
        wait(
            lambda: "已排队" in window.chat.toPlainText()
            or "等待知识库" in window.chat.toPlainText()
        )
        assert task.operation == "ContinueConversation"
        assert "第一轮的完整回答" in window.chat.toPlainText()
        assert window.stop_answer.isVisible()
        window.question.setPlainText("不要重复提交")
        QTest.keyClick(window.question, Qt.Key.Key_Return)
        assert len(window.manager.tasks()) == before + 1
        button(window, "新对话").click()
        window.question.setPlainText("新对话草稿")
        window.manager.stop(task.id)
    wait(lambda: task.id in window._seen_terminal)
    assert window.manager.get(task.id).state in TERMINAL
    assert window.conversations.active.identity is None
    assert window.chat.toPlainText() == ""
    assert window.question.toPlainText() == "新对话草稿"
    from openkb.application.conversations import read_conversation

    wait(
        lambda: any(
            q == "第三轮：在原对话中继续" for q, _ in read_conversation(kb, session.id).timeline
        )
    )
    assert len(read_conversation(kb, session.id).turns) == 2
    window._load_conversation(session.id)
    wait(lambda: "第三轮：在原对话中继续" in window.chat.toPlainText())
    window.question.setPlainText("切换历史后保留的草稿")
    another = ChatSession.new(kb, "openai/verification", "zh")
    another.record_turn("另一段历史", "另一个回答", [])
    window._load_conversation(another.id)
    wait(lambda: "另一个回答" in window.chat.toPlainText())
    window._load_conversation(session.id)
    wait(lambda: "第二轮的完整回答" in window.chat.toPlainText())
    assert window.question.toPlainText() == "切换历史后保留的草稿"
    window.question.setPlainText("请接着说明，保持完整上下文。")
    QApplication.processEvents()
    assert window.grab().save(str(root / "conversation-continuity.png"))
