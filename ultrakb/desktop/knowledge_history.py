"""Read immutable knowledge snapshots without changing the current view."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QListWidget, QPushButton, QVBoxLayout

from ultrakb.application.knowledge_history import history_pages, read_history_page
from ultrakb.application.refresh import list_knowledge_history
from ultrakb.application.views import view_scope
from ultrakb.desktop.reader import MarkdownView


class KnowledgeHistoryDialog(QDialog):
    def __init__(self, window, kb, view_id):
        super().__init__(window)
        self.window, self.kb, self.view_id = window, kb, view_id
        self._closed, self._generation = False, 0
        self._revision_generation = 0
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("历史知识 · 只读")
        self.resize(980, 760)
        layout = QVBoxLayout(self)
        self.revisions = QComboBox()
        self.revisions.setAccessibleName("历史知识修订")
        self.revisions.currentIndexChanged.connect(self.select_revision)
        layout.addWidget(self.revisions)
        self.more_revisions = QPushButton("更多历史")
        self.more_revisions.setEnabled(False)
        self.more_revisions.clicked.connect(self.load_more_history)
        layout.addWidget(self.more_revisions)
        self.pages = QListWidget()
        self.pages.setMaximumHeight(160)
        self.pages.currentTextChanged.connect(self.select_page)
        layout.addWidget(self.pages)
        self.more_pages = QPushButton("更多页面")
        self.more_pages.setEnabled(False)
        self.more_pages.clicked.connect(self.load_more_pages)
        layout.addWidget(self.more_pages)
        self.status = QLabel("正在读取历史…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.reader = MarkdownView()
        self.reader.anchorClicked.connect(self.follow_link)
        layout.addWidget(self.reader, 1)

        captured = getattr(window, "read_scope", None)
        self._history_scope = captured

        def read():
            scope = captured or view_scope(kb, view_id)
            return scope, list_knowledge_history(kb, scope=scope, cancelled=lambda: self._closed)

        def loaded(result, error):
            if error:
                self.status.setText(str(error))
                return
            self._history_scope, values = result
            self.append_history(values)
            self.select_revision()

        self.submit_read(read, loaded, obsolete=lambda: self._closed)

    def submit_read(self, operation, callback, *, obsolete):
        main = self._history_scope is not None and self._history_scope.knowledge is not None
        self.window.io.submit(
            operation, callback, kb=None if main else self.kb, snapshot=not main, obsolete=obsolete
        )

    def append_history(self, values):
        self.revisions.blockSignals(True)
        for value in values:
            kind = {
                "compile": "导入编译",
                "manual": "人工修改",
                "refresh": "显式刷新",
                "empty": "确认贡献为空",
                "withdrawn": "撤下资料",
            }.get(value["change_kind"], "知识更新")
            self.revisions.addItem(f"{kind} · {value['knowledge_revision_id']}", value)
        self.revisions.blockSignals(False)
        self.more_revisions.setEnabled(len(values) == 63)

    def load_more_history(self):
        value = self.revisions.itemData(self.revisions.count() - 1)
        after = value.get("publication_id", value["knowledge_revision_id"])
        self.more_revisions.setEnabled(False)

        def loaded(values, error):
            if error:
                self.status.setText(str(error))
                self.more_revisions.setEnabled(True)
            else:
                self.append_history(values)

        self.submit_read(
            lambda: list_knowledge_history(
                self.kb, scope=self._history_scope, after=after, cancelled=lambda: self._closed
            ),
            loaded,
            obsolete=lambda: self._closed,
        )

    def select_revision(self):
        self._revision_generation += 1
        self._generation += 1
        self.pages.clear()
        self.more_pages.setEnabled(False)
        value = self.revisions.currentData()
        if value:
            self.load_more_pages()

    def load_more_pages(self):
        value = self.revisions.currentData()
        if value is None:
            return
        revision_id, generation = value["knowledge_revision_id"], self._revision_generation
        after = self.pages.item(self.pages.count() - 1).text() if self.pages.count() else None
        self.more_pages.setEnabled(False)

        def loaded(values, error):
            if error:
                self.status.setText(str(error))
                self.more_pages.setEnabled(True)
                return
            first = self.pages.count() == 0
            self.pages.addItems(values)
            self.more_pages.setEnabled(len(values) == 63)
            if first and values:
                self.pages.setCurrentRow(0)
            elif first:
                self.reader.show_temporary("此修订没有知识页面。")
                self.status.setText("历史知识 · 当前修订已退休相关页面。")

        self.submit_read(
            lambda: history_pages(
                self.kb,
                revision_id,
                scope=self._history_scope,
                after=after,
                cancelled=lambda: self._closed or generation != self._revision_generation,
            ),
            loaded,
            obsolete=lambda: self._closed or generation != self._revision_generation,
        )

    def follow_link(self, url):
        if url.scheme() == "ultrakb":
            path = url.path(QUrl.ComponentFormattingOption.FullyDecoded).lstrip("/")
            path = path if path.endswith(".md") else f"{path}.md"
            matches = self.pages.findItems(path, Qt.MatchFlag.MatchExactly)
            if matches:
                self.pages.setCurrentItem(matches[0])
            else:
                self.select_page(path)
        elif not url.scheme() and url.fragment():
            from ultrakb.rendering.markdown import heading_anchor

            self.reader.scrollToAnchor(heading_anchor(url.fragment()))
        elif url.scheme() in {"http", "https"}:
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(url)

    def select_page(self, path):
        self._generation += 1
        generation = self._generation
        revision = self.revisions.currentData()
        if not path or not revision:
            return

        def read():
            return read_history_page(
                self.kb,
                revision["knowledge_revision_id"],
                path,
                scope=self._history_scope,
                cancelled=lambda: self._closed or generation != self._generation,
            )

        def loaded(value, error):
            if error:
                self.status.setText(str(error))
                return
            page, assets = value
            self.status.setText(
                "历史知识 · 实际来源修订：" + (", ".join(page.source_revision_ids) or "旧库未记录")
            )
            self.reader.show_markdown(
                page.body,
                assets,
                dark=self.window.appearance.dark,
                scale=self.window.zoom.currentData(),
            )

        self.submit_read(
            read,
            loaded,
            obsolete=lambda: self._closed or generation != self._generation,
        )

    def done(self, result):
        self._closed = True
        self.reader.stop_rendering()
        super().done(result)

    def closeEvent(self, event):
        self._closed = True
        self.reader.stop_rendering()
        super().closeEvent(event)
