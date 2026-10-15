"""Generic import controls shared by file, directory and URL ingestion."""

from ultrakb.desktop.form_controls import FocusComboBox


def source_metadata_form(window, layout, *, actions=None):
    from PySide6.QtWidgets import QFormLayout, QToolButton, QWidget

    form = QFormLayout()
    window.remote_assets = FocusComboBox()
    window.remote_assets.setAccessibleName("本次 HTML 远程图片")
    window.remote_assets.addItem("使用知识库 / 全局设置", None)
    window.remote_assets.addItem("下载并保留", True)
    window.remote_assets.addItem("保留链接，不下载", False)
    form.addRow("本次 HTML 远程图片", window.remote_assets)
    widget = QWidget()
    widget.setLayout(form)
    toggle = QToolButton()
    toggle.setText("导入选项")
    toggle.setAccessibleName("导入选项")
    toggle.setCheckable(True)
    toggle.toggled.connect(widget.setVisible)
    widget.hide()
    (actions if actions is not None else layout).addWidget(toggle)
    layout.addWidget(widget)
