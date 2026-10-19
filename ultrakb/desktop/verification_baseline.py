"""Exercise the original document artifacts through the native presentation layer."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel


def verify_baseline(window, kb, root, wait):
    from ultrakb.desktop.conversation_activity import task_status
    from ultrakb.desktop.source_reader import SourceReader
    from ultrakb.desktop.verification_data import retain_document
    from ultrakb.runtime.records import TaskView

    retain_document(kb, "短文.md", "短文", "# 短文\n\n原始 Markdown 正文。")
    publication = retain_document(
        kb,
        "长文.pdf",
        "长文",
        "第一页和第二页的保留正文。",
        pages=[
            {"page": 1, "content": "# 第一页\n\nPageIndex 保留的第一页。"},
            {"page": 2, "content": "# 第二页\n\nPageIndex 保留的第二页。"},
        ],
    )
    publication_id = publication.publication_id
    window.open_knowledge_base(kb)
    wait(
        lambda: window.kb == kb
        and window.read_scope is not None
        and window.read_scope.publication_id == publication_id
    )
    window.shell.navigate("资料")
    panel = window.workspaces.panels["资料"][0]
    wait(
        lambda: panel.table.rowCount() == 2
        and not panel._inventory_loading
        and panel.read_scope.publication_id == publication_id
    )
    assert panel.table.columnCount() == 4
    assert all("处理完成" in panel.table.item(row, 2).text() for row in range(2))
    assert not panel.details.isVisible()
    hint = window.findChild(QLabel, "documentWorkflow").text()
    assert "PDF" in hint and "Word" in hint
    for name, expected in (("短文.md", "原始 Markdown 正文"), ("长文.pdf", "保留的第二页")):
        row = next(i for i in range(2) if panel.table.item(i, 0).text().endswith(name))
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
    assert task_status(task) == "正在组织答案，等待模型响应"
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
    assert window.task_table.columnCount() == 6

    window.shell.navigate("设置")
    settings = window.workspaces.panels["当前知识库"][0]
    wait(lambda: settings._loaded)
    assert "pageindex_threshold" in settings.fields
    assert set(settings.fields) == {
        "model",
        "language",
        "pageindex_threshold",
        "pdf_short_max_pages",
        "model_capacity",
        "download_remote_assets",
        "office_runtime_path",
        "office_timeout_seconds",
        "extraction_budget",
        "entity_types",
        "openai_api_base",
        "api_key",
    }
