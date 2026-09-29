"""Inspect retained evidence and explicitly resume version clarifications."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from openkb.application.version_review import (
    cancel_version_review,
    list_version_reviews,
    review_source_version,
    supplement_version_reviews,
)
from openkb.desktop.views import source_metadata_form
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import ResumeVersionReview
from openkb.view_records import SourceMetadata

LABELS = {
    "source_identity": "同一来源",
    "product": "产品 / 主题",
    "applicable_versions": "适用版本",
    "family": "资料用途",
    "document_revision": "资料修订",
}


class VersionReviewsDialog(QDialog):
    def __init__(self, window, kb, *, scope=None, source_ids=()):
        super().__init__(window)
        self.window, self.kb, self.scope = window, kb, scope
        self._closed, self._busy, self._task = False, False, None
        self.setWindowTitle("版本信息待补充")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(880, 760)
        layout = QVBoxLayout(self)
        self.status = QLabel("查看标题依据后填写信息；可多选批量补充。空白字段保持原值。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self.selected)
        layout.addWidget(self.list)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        source_metadata_form(self, layout)
        actions = QHBoxLayout()
        self.buttons = []
        for text, callback in (
            ("刷新", self.reload),
            ("保存所选信息", self.supplement),
            ("取消所选待补", self.cancel),
            ("继续编译所选资料", self.resume),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self.buttons.append(button)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        if source_ids:
            self.run(
                lambda: [
                    review_source_version(kb, identity, scope=scope) for identity in source_ids
                ],
                "已打开资料版本信息；保存后点击继续编译。",
            )
        else:
            self.reload()

    def reviews(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()]

    def selected(self):
        blocks = []
        for review in self.reviews():
            fields = "、".join(LABELS.get(name, name) for name in review.missing_fields)
            lines = [review.name, f"待补字段：{fields or '已齐全'}", review.reason]
            for related in review.related_sources:
                lines.append(
                    f"相关资料：{related.name} · {related.metadata.product or '未知产品'} · "
                    f"{', '.join(related.metadata.applicable_versions) or '未知版本'} · "
                    f"{related.metadata.family or '未知用途'}"
                )
                lines.append(
                    "匹配依据：" + "、".join(LABELS[name] for name in related.matching_fields)
                )
                for name in related.matching_fields:
                    if name in related.evidence:
                        lines.append(f"{LABELS[name]}出处：{related.evidence[name]}")
            for field, label in LABELS.items():
                if field == "source_identity":
                    continue
                value = getattr(review.metadata, field)
                lines.append(
                    f"{label}：{', '.join(value) if isinstance(value, tuple) else value or '未知'}"
                )
            for candidate in review.candidates:
                lines.append(f"候选 {LABELS[candidate.field]}：{', '.join(candidate.values)}")
                lines.append(f"依据：{candidate.location} · {candidate.excerpt}")
            blocks.append("\n".join(lines))
        self.details.setPlainText("\n\n".join(blocks))
        self.buttons[-1].setEnabled(
            not self._busy
            and not self._task
            and bool(self.reviews())
            and all(review.status == "ready" for review in self.reviews())
        )

    def reload(self):
        if self._busy or self._task:
            return
        self._busy = True

        def loaded(reviews, error):
            self._busy = False
            if error:
                self.status.setText(str(error))
                return
            self.list.clear()
            for review in reviews:
                item = QListWidgetItem(
                    f"{review.name} · {'可继续编译' if review.status == 'ready' else '待补充'}"
                )
                item.setData(Qt.ItemDataRole.UserRole, review)
                self.list.addItem(item)
            self.selected()

        self.window.io.submit(
            lambda: list_version_reviews(self.kb, scope=self.scope),
            loaded,
            kb=self.kb,
            obsolete=lambda: self._closed,
        )

    def run(self, operation, message):
        if self._busy or self._task:
            return
        self._busy = True
        for button in self.buttons:
            button.setEnabled(False)

        def loaded(value, error):
            self._busy = False
            for button in self.buttons:
                button.setEnabled(True)
            self.status.setText(str(error) if error else message)
            self.reload()

        self.window.io.submit(
            operation, loaded, kb=self.kb, exclusive=True, obsolete=lambda: self._closed
        )

    def supplement(self):
        values = {
            name: field.text().strip()
            for name, field in self.source_metadata_fields.items()
            if field.text().strip()
        }
        if not values or not self.reviews():
            self.status.setText("请选择资料并填写要补充的字段。")
            return
        if "applicable_versions" in values:
            values["applicable_versions"] = tuple(
                part.strip()
                for part in values["applicable_versions"].replace("，", ",").split(",")
                if part.strip()
            )
        try:
            metadata = SourceMetadata.model_validate(values)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        updates = {review.review_id: metadata for review in self.reviews()}
        self.run(
            lambda: supplement_version_reviews(self.kb, updates, scope=self.scope),
            "信息已保存；点击继续编译开始新的处理。",
        )

    def cancel(self):
        identities = [review.review_id for review in self.reviews()]
        if identities:
            self.run(
                lambda: [
                    cancel_version_review(self.kb, identity, scope=self.scope)
                    for identity in identities
                ],
                "已取消所选待补；不会自动恢复。",
            )

    def resume(self):
        reviews = self.reviews()
        if (
            not reviews
            or self._busy
            or self._task
            or any(review.status != "ready" for review in reviews)
        ):
            return
        self._task = self.window.manager.submit(
            self.kb,
            [ResumeVersionReview(review.review_id, view_id=review.view_id) for review in reviews],
        )
        self.status.setText("已提交编译；原件和解析结果将复用。可在任务面板查看进度。")
        for button in self.buttons:
            button.setEnabled(False)

    def poll(self):
        if not self._task:
            return
        task = self.window.manager.get(self._task)
        if task.state not in TERMINAL:
            return
        self._task = None
        self.status.setText(
            "\n".join(result.error or result.status for result in task.results)
            or task.error
            or task.state
        )
        for button in self.buttons:
            button.setEnabled(True)
        self.reload()
        if self.window.kb == self.kb:
            self.window._refresh()

    def done(self, result):
        self._closed = True
        self.timer.stop()
        super().done(result)
