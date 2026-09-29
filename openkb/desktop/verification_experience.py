"""Exercise live answer cards and the knowledge/creation workflow in actual Qt widgets."""

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QLabel


def verify_experience(window, kb, root, wait):
    from openkb.agent.token_usage import TokenUsage
    from openkb.desktop.conversations import Chat
    from openkb.rendering.markdown import render_markdown
    from openkb.runtime.records import TaskView

    document = QTextDocument()
    for code in ("printf hello", "one\n\ntwo", "one\n"):
        document.setHtml(render_markdown(f"```bash\n{code}\n```", None).html)
        assert document.toPlainText() == code

    directory = window.workspaces.knowledge
    paths = [entry.path for entry in directory.entries]
    assert paths and not any(p in paths for p in ("index.md", "log.md", "AGENTS.md"))
    directory.search.setText("不会匹配的知识检索")
    assert "没有匹配" in directory.summary.text()
    directory.search.clear()
    assert "篇知识" in directory.summary.text()
    assert all(
        directory.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) is None
        for i in range(directory.tree.topLevelItemCount())
    )
    tasks_before = len(window.manager.tasks())
    window.workspaces.ask_page.click()
    assert f"[[{window.page.path}]]" in window.question.toPlainText()
    assert len(window.manager.tasks()) == tasks_before

    controller = window.conversations
    chat = Chat(
        kb,
        turns=[("上一轮", "这条历史回答保持原位。")],
        question="演示流式回答",
        running=True,
        task="f" * 32,
    )
    controller.select(chat)
    controller.tasks[chat.task] = chat
    task = TaskView(
        chat.task, str(kb), "ContinueConversation", "running", "answering", 1, (), False, False
    )
    wait(lambda: "历史回答" in window.chat.toPlainText())
    history_reader = window.chat._entries[1][-1]
    answer = (
        "这是实时逐步出现的回答。\n\n```python\nprint('hello')\n```\n\n接下来可以继续探索相关知识。"
    )
    usage = TokenUsage(1200, 340, 96, 24).to_dict()
    controller.observe(replace(task, text="<think>hidden reasoning</think>" + answer, usage=usage))
    assert answer not in window.chat.toPlainText(), "incoming text must be progressively revealed"
    wait(lambda: "实时" in window.chat.toPlainText())
    live_reader = window.chat._entries[-1][-1]
    controller.observe(replace(task, text=answer + " 更多内容。", usage=usage))
    assert window.chat._entries[1][-1] is history_reader
    assert window.chat._entries[-1][-1] is live_reader
    wait(lambda: "更多内容" in window.chat.toPlainText())
    assert "hidden reasoning" not in window.chat.toPlainText()
    assert "输入命中缓存 1,200" in window.token_usage.text()
    assert "思考 24" in window.token_usage.text()
    assert not any(label.text() == "对话自动保存" for label in window.findChildren(QLabel))
    chat.running = False
    chat.turns.append((chat.question, answer + " 更多内容。"))
    chat.question = chat.answer = ""
    controller.render()
    wait(lambda: live_reader._source.endswith("更多内容。") and live_reader.rendering_stopped())
    window.grab().save(str(root / "chat-streaming-usage.png"))
    controller.new()
    assert window.chat.toPlainText() == "" and "1,200" not in window.token_usage.text()
    controller.tasks.pop(task.id)
    # A provider may finish before the first UI poll: reveal that answer too.
    fallback = Chat(kb, question="快速回答", running=True)
    controller.select(fallback)
    fallback.running = False
    fallback.turns = [(fallback.question, "一次性返回的回答也逐步显示。")]
    fallback.question = ""
    controller.render()
    typing = window.chat._entries[-1][-1]._typing
    assert typing.isActive()
    controller.new()
    assert not typing.isActive(), "switching conversations must stop old animation"

    window.shell.navigate("产物")
    panel = window.workspaces.panels["产物"][0]
    wait(panel.isVisible)
    panel.new_button.click()
    assert panel.brief.isVisible()
    panel.kind.setCurrentIndex(1)
    assert "演示文稿" in panel.kind_hint.text()
    panel.intent.setPlainText("面向新同事，介绍知识库的核心概念，准备一次十分钟分享。")
    panel.new_button.click()
    panel.new_button.click()
    assert "新同事" in panel.intent.toPlainText()
    panel.search.setText("不会匹配的成果")
    assert panel.empty_hint.isVisible()
    panel.search.clear()
    window.grab().save(str(root / "creation-workbench.png"))
    panel.new_button.setChecked(False)
