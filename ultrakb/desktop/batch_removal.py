"""Review aggregate consequences and bind each group to explicit confirmation."""

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from ultrakb.application.batch_removal import preview_batch_removal
from ultrakb.runtime.requests import RemoveDocuments


class RemovalReview(QDialog):
    def __init__(self, preview, parent):
        super().__init__(parent)
        self.preview, self._epoch, self._closed, self._loading = preview, 0, False, False
        self.finished.connect(lambda _: setattr(self, "_closed", True))
        self.setWindowTitle("确认移除所选资料")
        self.resize(720, 600)
        layout = QVBoxLayout(self)
        title = QLabel(
            f"目标：{preview.kb_dir}\n移除 {len(preview.identities)} 项资料；请分别核对后果。"
        )
        title.setWordWrap(True)
        layout.addWidget(title)
        note = QLabel(
            "新版来源保留原件和历史，相关知识待更新。旧资料按下方选项清理，旧链接可能不可读。"
            "此操作不调用模型，不删除外部原文件。已完成项不会撤销。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        kinds = {item.kind for item in preview.identities}
        self.keep_raw = QCheckBox("仅旧式资料：保留知识库中的原始文件副本")
        self.keep_empty = QCheckBox("仅旧式资料：保留失去来源的空页面")
        for option in (self.keep_raw, self.keep_empty):
            option.setVisible("legacy" in kinds)
            layout.addWidget(option)
        self.keep_raw.setChecked(preview.keep_raw)
        self.keep_empty.setChecked(preview.keep_empty)
        self.acknowledgements = {}
        for kind, text in (
            ("source", "已确认新版来源：撤下有效贡献，保留原件和历史"),
            ("legacy", "已确认旧式资料：按所选选项清理，旧链接可能不可读"),
        ):
            if kind in kinds:
                box = QCheckBox(text)
                box.setVisible(len(kinds) > 1)
                box.setChecked(len(kinds) == 1)
                self.acknowledgements[kind] = box
                layout.addWidget(box)
                box.toggled.connect(self.update_confirmation)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm.setText("确认移除这些资料")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.keep_raw.toggled.connect(self.options_changed)
        self.keep_empty.toggled.connect(self.options_changed)
        self.render_preview()

    def update_confirmation(self):
        if hasattr(self, "confirm"):
            self.confirm.setEnabled(
                not self._loading and all(box.isChecked() for box in self.acknowledgements.values())
            )

    def render_preview(self):
        preview = self.preview
        lines = []
        for kind, heading in (
            ("source", "新版来源：撤下有效贡献，原件和历史保留"),
            ("legacy", "旧式资料：按批准范围清理，旧链接可能不可读"),
        ):
            names = [
                name
                for name, item in zip(preview.names, preview.identities, strict=True)
                if item.kind == kind
            ]
            if names:
                lines += [heading, *(f"移除：{name}" for name in names)]
        for label, values in (
            ("知识待更新", preview.affected_pages),
            ("将失去全部有效来源", preview.lose_all_sources),
            ("旧资料将失去全部登记来源", preview.legacy_empty),
            ("旧式删除", preview.legacy_deleted),
            ("旧式修改", preview.legacy_modified),
            ("旧式保留", preview.legacy_retained),
            ("保留原件", preview.originals),
            ("保留引用 / 历史", preview.retained),
        ):
            lines += [f"{label}：{path}" for path in values]
        if not preview.references_complete:
            lines.append("部分引用无法核对；保守保留存储内容，待人工核查。")
        self.details.setPlainText("\n".join(lines))
        self.update_confirmation()

    def options_changed(self):
        self._epoch += 1
        epoch = self._epoch
        self._loading = True
        for box in self.acknowledgements.values():
            box.setChecked(False)
        self.confirm.setEnabled(False)
        self.details.setPlainText("选项已变化，正在重新计算整个选择集；原确认已失效。")
        parent = self.parent()
        root, selected, scope = Path(self.preview.kb_dir), self.preview.identities, parent.scope
        keep_raw, keep_empty = self.keep_raw.isChecked(), self.keep_empty.isChecked()

        def loaded(preview, error):
            if error:
                self.details.setPlainText(str(error))
                return
            self._loading = False
            self.preview = preview
            if len(self.acknowledgements) == 1:
                next(iter(self.acknowledgements.values())).setChecked(True)
            self.render_preview()

        parent.window.io.submit(
            lambda: preview_batch_removal(
                root, selected, scope=scope, keep_raw=keep_raw, keep_empty=keep_empty
            ),
            loaded,
            kb=root,
            obsolete=lambda: self._closed or parent._closed or epoch != self._epoch,
        )


def execute(panel, identities):
    if panel._task or panel._batch_preparing or not identities:
        return
    root, scope = panel.kb, panel.scope
    panel._batch_preparing = True
    panel.status.setText("正在计算整个选择集的共享知识影响…")

    def loaded(preview, error):
        panel._batch_preparing = False
        if error:
            panel.status.setText(str(error))
            return
        dialog = RemovalReview(preview, panel)
        if (
            dialog.exec() != QDialog.DialogCode.Accepted
            or not dialog.confirm.isEnabled()
            or panel._closed
            or panel.window.kb != root
        ):
            return
        preview = dialog.preview
        panel._task = panel.window.manager.submit(
            root,
            [
                RemoveDocuments(
                    preview.encode(),
                    view_id=preview.view_id,
                    confirmed_groups=tuple(
                        kind for kind, box in dialog.acknowledgements.items() if box.isChecked()
                    ),
                )
            ],
        )
        panel.status.setText(
            f"已提交 {len(preview.identities)} 项移除；可在任务页停止并查看逐项结果。"
        )
        from ultrakb.desktop.document_selection import update

        update(panel)

    panel.window.io.submit(
        lambda: preview_batch_removal(root, identities, scope=scope),
        loaded,
        kb=root,
        obsolete=lambda: panel._closed or panel.window.kb != root,
    )
