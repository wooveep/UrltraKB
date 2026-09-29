"""Exercise the original document artifacts through the native presentation layer."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel


def verify_baseline(window, kb, root, wait):
    from openkb.desktop.conversation_activity import task_status
    from openkb.desktop.source_reader import SourceReader
    from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock
    from openkb.runtime.records import TaskView

    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(kb / "wiki/sources/短文.md", "# 短文\n\n原始 Markdown 正文。")
        atomic_write_json(
            kb / "wiki/sources/长文.json",
            [
                {"page": 1, "content": "# 第一页\n\nPageIndex 保留的第一页。"},
                {"page": 2, "content": "# 第二页\n\nPageIndex 保留的第二页。"},
            ],
        )
        atomic_write_json(
            kb / ".openkb/hashes.json",
            {
                "a" * 64: {"name": "短文.docx", "doc_name": "短文", "type": "docx"},
                "b" * 64: {"name": "长文.pdf", "doc_name": "长文", "type": "long_pdf"},
            },
        )
    window.open_knowledge_base(kb)
    wait(lambda: window.kb == kb and not window.io._callbacks)
    window.shell.navigate("资料")
    panel = window.workspaces.panels["资料"][0]
    wait(lambda: panel.table.rowCount() == 2)
    assert panel.table.item(0, 2).text() == "Markdown 全文编译"
    assert panel.table.item(1, 2).text() == "PageIndex 长文索引"
    hint = window.findChild(QLabel, "documentWorkflow").text()
    assert "Markdown" in hint and "PageIndex" in hint
    for row, expected in ((0, "原始 Markdown 正文"), (1, "保留的第二页")):
        panel.table.selectRow(row)
        panel.read_button.click()
        wait(lambda: any(d.isVisible() for d in window.findChildren(SourceReader)))
        dialog = next(d for d in window.findChildren(SourceReader) if d.isVisible())
        wait(lambda: expected in dialog.reader.toPlainText())
        assert dialog.reader.isReadOnly()
        assert dialog.windowModality() == Qt.WindowModality.WindowModal
        dialog.grab().save(str(root / f"source-{row}.png"))
        dialog.close()
        wait(lambda: not any(d.isVisible() for d in window.findChildren(SourceReader)))
    for width in (900, 1366):
        window.resize(width, 768)
        window.grab().save(str(root / f"documents-{width}.png"))

    task = TaskView(
        "c" * 32, str(kb), "ContinueConversation", "running", "answering", 1, (), False, False
    )
    assert task_status(task) == "正在查阅知识库并整理回答"
    window.shell.navigate("对话")
    window.conversation_notice.set_task(task)
    window.conversation_notice.show()
    assert "核对" not in window.conversation_notice.steps.text()
    window.chat.show_turns(
        [("如何使用知识库？", "先导入资料，再阅读知识与继续提问。")], kb / "wiki"
    )
    wait(lambda: "先导入资料" in window.chat.toPlainText())
    window.grab().save(str(root / "baseline-chat.png"))
    window.conversation_notice.setText("")
    assert window.task_table.columnCount() == 5

    window.shell.navigate("设置")
    settings = window.workspaces.panels["当前知识库"][0]
    wait(lambda: settings._loaded)
    assert "pageindex_threshold" in settings.fields
    assert set(settings.fields) == {
        "model",
        "language",
        "pageindex_threshold",
        "entity_types",
        "openai_api_base",
        "api_key",
    }
