"""Generic import controls shared by file, directory and URL ingestion."""

from openkb.desktop.form_controls import FocusComboBox


def source_metadata_form(window, layout):
    from PySide6.QtWidgets import QFormLayout, QWidget

    form = QFormLayout()
    window.remote_assets = FocusComboBox()
    window.remote_assets.setAccessibleName("本次 HTML 远程图片")
    window.remote_assets.addItem("使用知识库 / 全局设置", None)
    window.remote_assets.addItem("下载并保留", True)
    window.remote_assets.addItem("保留链接，不下载", False)
    form.addRow("本次 HTML 远程图片", window.remote_assets)
    widget = QWidget()
    widget.setLayout(form)
    layout.addWidget(widget)
