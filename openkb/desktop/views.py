"""Choose a persistent knowledge view without changing queued requests."""

from openkb.desktop.form_controls import FocusComboBox


class ViewPicker(FocusComboBox):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setAccessibleName("知识视图")
        self.setMaximumWidth(260)
        self.activated.connect(self.select_view)
        self.set_views(())

    def set_views(self, views):
        self.blockSignals(True)
        self.clear()
        self.addItem("未指定 · 问答按系列默认", None)
        self.setToolTip("未指定时导入自动判断版本；问答按各系列默认版本取证据，无默认则分别回答。")
        for view in views:
            label = (
                "旧知识 · legacy"
                if view.view_id == "legacy"
                else (
                    f"{view.product or '未知产品'} · "
                    f"{', '.join(view.applicable_versions) or '未知版本'}"
                )
            )
            self.addItem(f"{label} · {view.view_id[:8]}", view.view_id)
        self.setCurrentIndex(max(0, self.findData(self.window.view_id)))
        self.blockSignals(False)

    def select_view(self):
        window = self.window
        identity = self.currentData()
        if window.kb is None or identity == window.view_id:
            return
        window._keep_draft()
        window.view_id = identity
        window.page = None
        window._page_request_id += 1
        window.page_context.clear()
        window.editor.clear()
        window.reader.show_temporary("正在读取所选知识视图…")
        window.conversations.reset()
        window.workspaces.reset()
        window._refresh()


def source_metadata_form(window, layout):
    from PySide6.QtWidgets import QFormLayout, QLineEdit, QWidget

    form = QFormLayout()
    fields = {}
    for key, label in (
        ("product", "产品（可选）"),
        ("applicable_versions", "完整适用版本（逗号分隔）"),
        ("family", "资料系列（如安装手册）"),
        ("document_revision", "资料修订标识（如 R2）"),
    ):
        field = QLineEdit()
        field.setAccessibleName(label)
        form.addRow(label, field)
        fields[key] = field
    window.remote_assets = FocusComboBox()
    window.remote_assets.setAccessibleName("本次 HTML 远程图片")
    window.remote_assets.addItem("使用知识库 / 全局设置", None)
    window.remote_assets.addItem("下载并保留", True)
    window.remote_assets.addItem("保留链接，不下载", False)
    form.addRow("本次 HTML 远程图片", window.remote_assets)
    widget = QWidget()
    widget.setLayout(form)
    layout.addWidget(widget)
    window.source_metadata_fields = fields


def import_metadata(window):
    from openkb.view_records import SourceMetadata

    values = {key: field.text().strip() for key, field in window.source_metadata_fields.items()}
    if not any(values.values()):
        return None
    supplied = {name: value for name, value in values.items() if value}
    if values["applicable_versions"]:
        supplied["applicable_versions"] = tuple(
            part.strip()
            for part in values["applicable_versions"].replace("，", ",").split(",")
            if part.strip()
        )
    return SourceMetadata.model_validate(supplied)
