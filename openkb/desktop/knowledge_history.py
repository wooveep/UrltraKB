"""Read immutable knowledge snapshots without changing the current view."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QListWidget, QVBoxLayout

from openkb.application.pages import read_page
from openkb.application.refresh import list_knowledge_history
from openkb.application.views import view_scope
from openkb.desktop.reader import MarkdownView


class KnowledgeHistoryDialog(QDialog):
    def __init__(self, window, kb, view_id):
        super().__init__(window)
        self.window, self.kb, self.view_id = window, kb, view_id
        self._closed, self._generation = False, 0
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("历史知识 · 只读")
        self.resize(980, 760)
        layout = QVBoxLayout(self)
        self.revisions = QComboBox()
        self.revisions.setAccessibleName("历史知识修订")
        self.revisions.currentIndexChanged.connect(self.select_revision)
        layout.addWidget(self.revisions)
        self.pages = QListWidget()
        self.pages.setMaximumHeight(160)
        self.pages.currentTextChanged.connect(self.select_page)
        layout.addWidget(self.pages)
        self.status = QLabel("正在读取历史…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.reader = MarkdownView()
        self.reader.anchorClicked.connect(self.follow_link)
        layout.addWidget(self.reader, 1)

        def read():
            return list_knowledge_history(kb, scope=view_scope(kb, view_id))

        def loaded(values, error):
            if error:
                self.status.setText(str(error))
                return
            self.revisions.blockSignals(True)
            for value in values:
                kind = {"compile": "导入编译", "manual": "人工修改", "refresh": "显式刷新"}[
                    value["change_kind"]
                ]
                self.revisions.addItem(f"{kind} · {value['knowledge_revision_id']}", value)
            self.revisions.blockSignals(False)
            self.select_revision()

        window.io.submit(read, loaded, kb=kb, obsolete=lambda: self._closed)

    def select_revision(self):
        self._generation += 1
        self.pages.clear()
        value = self.revisions.currentData()
        if value:
            self.pages.addItems(value["pages"])
            self.pages.setCurrentRow(0)
            if not value["pages"]:
                self.reader.show_temporary("此修订没有知识页面。")
                self.status.setText("历史知识 · 当前修订已退休相关页面。")

    def follow_link(self, url):
        if url.scheme() == "openkb":
            path = url.path(QUrl.ComponentFormattingOption.FullyDecoded).lstrip("/")
            path = path if path.endswith(".md") else f"{path}.md"
            matches = self.pages.findItems(path, Qt.MatchFlag.MatchExactly)
            if matches:
                self.pages.setCurrentItem(matches[0])
            else:
                self.status.setText("此历史修订没有链接指向的页面。")
        elif not url.scheme() and url.fragment():
            from openkb.rendering.markdown import heading_anchor

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
            scope = view_scope(
                self.kb, self.view_id, historical_revision=revision["knowledge_revision_id"]
            )
            return read_page(self.kb, path, scope=scope), scope

        def loaded(value, error):
            if error:
                self.status.setText(str(error))
                return
            page, scope = value
            self.status.setText(
                "历史知识 · 实际来源修订：" + (", ".join(page.source_revision_ids) or "旧库未记录")
            )
            self.reader.show_markdown(
                page.body,
                (scope.wiki_dir / path).parent,
                dark=self.window.appearance.dark,
                scale=self.window.zoom.currentData(),
            )

        self.window.io.submit(
            read,
            loaded,
            kb=self.kb,
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
