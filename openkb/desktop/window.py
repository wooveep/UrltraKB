"""Native workbench: KB navigation, reading/editing, questions, and task observation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QTableWidgetItem,
    QTreeWidgetItem,
)

from openkb.agent.chat_session import list_sessions
from openkb.application.catalog import knowledge_bases
from openkb.application.knowledge_bases import get_kb_list, get_kb_status, initialize_kb, open_kb
from openkb.application.pages import Page, read_page
from openkb.application.reading import read_page_context
from openkb.config import GLOBAL_CONFIG_DIR
from openkb.desktop.brand import NAME, application_icon
from openkb.desktop.editor import DraftDialog, PageDraft
from openkb.desktop.io import LocalIO
from openkb.desktop.reader import MarkdownView
from openkb.inputs import SUPPORTED_EXTENSIONS
from openkb.page_ops import EDITABLE_SECTIONS
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import (
    ImportFile,
    ImportUrl,
    SavePage,
)
from openkb.runtime.tasks import TaskManager
from openkb.runtime.watch import NativeWatchRegistry

_STATES = {
    "queued": "排队中",
    "waiting": "等待知识库",
    "running": "执行中",
    "stopping": "正在安全停止",
    "completed": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "stopped": "已停止",
    "interrupted": "意外中断",
    "blocked": "需要修复",
}
_OPERATIONS = {
    "SavePage": "保存页面",
    "AskQuestion": "问答",
    "ContinueConversation": "对话",
    "ImportFile": "导入资料",
    "RemoveDocument": "删除资料",
    "RecompileDocument": "重编译资料",
    "ImportUrl": "导入网址",
    "DeleteConversation": "删除对话",
    "ExportConversation": "导出对话",
    "CheckKnowledge": "知识检查与修复",
    "ReparseSource": "重新解析",
    "ContinueSource": "继续编译",
    "ReprocessSourcePage": "重新识别页面",
    "RebuildSourceNavigation": "重建资料导航",
}

_STAGES = {
    "parsing": "解析资料",
    "facts": "提取知识",
    "planning": "规划知识页面",
    "generation": "生成知识页面",
    "verification": "核对知识",
    "compiling": "知识编译",
    "committing": "保存结果",
    "committed": "已保存",
    "waiting-input": "等待文件稳定",
    "input-returned-to-watch": "输入仍在变化，已交回监听补查",
}


class Workbench(QMainWindow):
    def __init__(self, *, history_dir: Path | None = None):
        super().__init__()
        self.setWindowTitle(NAME)
        self.resize(1320, 900)
        self.kb: Path | None = None
        self.page: Page | None = None
        self._drafts: dict[tuple[str, str], PageDraft] = {}
        self._save_tasks: dict[str, tuple[tuple[str, str], str]] = {}
        self._seen_terminal: set[str] = set()
        self._deleting_kbs: set[Path] = set()
        self._kb_deletion_versions: dict[Path, int] = {}
        self._page_request_id = 0
        self._open_request_id = 0
        self._pending_open: Path | None = None
        self._chat_task: str | None = None
        self._quitting = False
        self.io = LocalIO(self)
        self.manager = TaskManager(history_dir=history_dir or GLOBAL_CONFIG_DIR / "desktop/tasks")
        self.watch_registry = NativeWatchRegistry(self.manager)
        from openkb.desktop.layout import build_workbench

        build_workbench(self)
        self._build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_tasks)
        self.timer.start(150)
        self.io.submit(knowledge_bases, self._recent_loaded, global_settings=True)
        self.reader.show_markdown(
            "# UrltraKB\n\n打开已有知识库，或在您选择的位置创建一个。\n\n"
            "资料、页面、对话和任务始终归属于各自的知识库。",
            Path.cwd(),
        )

    def _watch(self):
        self.shell.navigate("任务")
        self.workspaces.task_tabs.setCurrentIndex(1)

    def _about(self):
        from openkb.desktop.about import AboutDialog

        AboutDialog(self.io, self).exec()

    def _settings(self, *, global_defaults=False):
        self.shell.navigate("设置")
        self.workspaces.settings_tabs.setCurrentIndex(0 if global_defaults else 1)

    def _documents(self):
        self.shell.navigate("资料")

    def _knowledge_bases(self):
        from openkb.desktop.knowledge_bases import KnowledgeBasesDialog

        KnowledgeBasesDialog(self).exec()

    def _knowledge_base_deleting(self, root, deleting):
        if deleting:
            self._deleting_kbs.add(root)
            self._kb_deletion_versions[root] = self._kb_deletion_versions.get(root, 0) + 1
            if self.kb == root or self._pending_open == root:
                self._open_request_id += 1
                self._pending_open = None
            if self.kb == root:
                self._page_request_id += 1
        else:
            self._deleting_kbs.discard(root)

    def _removed_knowledge_base(self, root):
        self._drafts = {key: draft for key, draft in self._drafts.items() if key[0] != str(root)}
        if self.kb != root:
            return
        self.kb = self.page = None
        self._open_request_id += 1
        self._page_request_id += 1
        self._chat_task = None
        self.pages.clear()
        self.page_context.clear()
        self.editor.clear()
        self.save_button.setEnabled(False)
        self.reader.show_temporary("知识库已删除。请选择或创建另一个知识库。")
        self.conversations.reset()
        self.location.setText("尚未打开知识库")
        self.setWindowTitle(NAME)
        self.workspaces.reset()

    def _task_details(self):
        task_id = self._selected_task()
        if task_id:
            from openkb.desktop.task_details import show_task_details

            try:
                task = self.manager.get(task_id)
            except KeyError:
                return
            show_task_details(task, self, manager=self.manager)

    def _maintenance(self):
        if self.kb is not None:
            from openkb.desktop.maintenance import MaintenanceDialog

            MaintenanceDialog(self, self.kb).exec()

    def _manage_artifacts(self):
        self.shell.navigate("产物")

    def _diagnose(self, path=None):
        from openkb.desktop.diagnostics import DiagnosticsDialog

        if path is None:
            selected = QFileDialog.getExistingDirectory(self, "选择要诊断的知识库")
            if not selected:
                return
            path = Path(selected)
        DiagnosticsDialog(self, path).exec()

    def _manage_sessions(self):
        self.shell.navigate("对话")
        self.workspaces.history_toggle.setChecked(True)
        self.workspaces.toggle_history()

    def _build_tray(self):
        icon = application_icon()
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(NAME)
        menu = QMenu(self)
        menu.addAction(f"显示 {NAME}", self._show_window)
        menu.addAction("退出", self.request_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show_window())
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _presentation_changed(self):
        if hasattr(self, "appearance"):
            self.appearance.present_readers()

    def _error(self, error):
        if error:
            QMessageBox.warning(self, "操作未完成", str(error))
            return True
        return False

    def _recent_loaded(self, values, error):
        if not error:
            self.kbs.blockSignals(True)
            self.kbs.clear()
            for label, path in values:
                self.kbs.addItem(label, str(path))
                self.kbs.setItemData(self.kbs.count() - 1, str(path), Qt.ItemDataRole.ToolTipRole)
            self.kbs.setCurrentIndex(self.kbs.findData(str(self.kb)) if self.kb else -1)
            self.kbs.blockSignals(False)

    def _choose_kb(self):
        path = QFileDialog.getExistingDirectory(self, "打开知识库")
        if path:
            self.open_knowledge_base(Path(path))

    def _create_kb(self):
        path = QFileDialog.getExistingDirectory(self, "选择新知识库的空目录")
        if path:
            self.io.submit(
                lambda: initialize_kb(Path(path), seed_environment=False, require_empty=True),
                lambda result, error: None
                if self._error(error)
                else self.open_knowledge_base(Path(path)),
                kb=Path(path),
                exclusive=True,
                creating=True,
                global_settings=True,
            )

    def open_knowledge_base(self, path: Path):
        path = path.expanduser().resolve()
        if path in self._deleting_kbs:
            return
        self._open_request_id += 1
        request_id = self._open_request_id
        self._pending_open = path

        def loaded(value, error):
            if request_id == self._open_request_id:
                self._pending_open = None
                self._opened(value, error, attempted=path)

        if self.kb:
            self.kbs.setCurrentIndex(self.kbs.findData(str(self.kb)))
        self.statusBar().showMessage(f"正在打开 {path}；若有写入任务，将等待其安全完成。")
        self.io.submit(
            lambda: open_kb(path),
            loaded,
            kb=path,
            exclusive=True,
            global_settings=True,
            obsolete=lambda: request_id != self._open_request_id,
        )

    def _opened(self, root, error, *, attempted=None):
        from openkb.mutation import RecoveryRequired

        if isinstance(error, RecoveryRequired) and attempted is not None:
            self._diagnose(attempted)
            return
        if self._error(error):
            return
        self._keep_draft()
        self.kb, self.page = root, None
        self.page_context.clear()
        self._page_request_id += 1
        self._chat_task = None
        self.reader.show_temporary("正在读取当前知识库…")
        self.conversations.reset()
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self.location.setText(str(root))
        self.setWindowTitle(f"{root.name} — {NAME}")
        index = self.kbs.findData(str(root))
        if index < 0:
            self.kbs.addItem(root.name, str(root))
            self.kbs.setItemData(self.kbs.count() - 1, str(root), Qt.ItemDataRole.ToolTipRole)
            index = self.kbs.count() - 1
        self.kbs.setCurrentIndex(index)
        self.kbs.setToolTip(str(root))
        self.workspaces.reset()
        self.conversations.recover(root)

    def _refresh(self):
        if self.kb is None or self.kb in self._deleting_kbs:
            return
        root = self.kb
        request_id = self._open_request_id

        def obsolete():
            return (
                root != self.kb or request_id != self._open_request_id or root in self._deleting_kbs
            )

        def read():
            return (
                sorted(
                    p.relative_to(root / "wiki").as_posix() for p in (root / "wiki").rglob("*.md")
                ),
                list_sessions(root),
                get_kb_list(root),
                get_kb_status(root),
            )

        def loaded(value, error):
            if obsolete() or self._error(error):
                return
            pages, sessions, info, status = value
            self.workspaces.overview_loaded(info, status)
            self.pages.clear()
            groups = {}
            for path in pages:
                group = path.split("/")[0] if "/" in path else "知识库"
                if group not in groups:
                    groups[group] = QTreeWidgetItem(self.pages, [group])
                item = QTreeWidgetItem(groups[group], [Path(path).stem])
                item.setData(0, Qt.ItemDataRole.UserRole, path)
            self.pages.expandToDepth(0)
            self.conversations.catalog_loaded(sessions)
            self.statusBar().showMessage(f"{root.name} · {info.get('document_count', 0)} 份资料")
            if self.page is None and (root / "wiki/index.md").exists():
                self.open_page("index.md", activate=False)

        self.io.submit(read, loaded, kb=root, global_settings=True, obsolete=obsolete)

    def _refresh_current(self):
        self._refresh()
        if self.page:
            self.open_page(self.page.path, activate=False)

    def _activate_page(self, item, column):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self.open_page(path)

    def open_page(self, path: str, anchor: str = "", *, activate=True):
        if self.kb is None or self.kb in self._deleting_kbs:
            return
        root = self.kb
        self._page_request_id += 1
        request_id = self._page_request_id
        self._keep_draft()

        def loaded(context, error):
            if self.kb != root or request_id != self._page_request_id or self._error(error):
                return
            page = context.page
            self.page = page
            self.page_context.show_context(root, context)
            editable = page.path.split("/")[0] in EDITABLE_SECTIONS
            self.save_button.setEnabled(editable)
            self.editor.setReadOnly(not editable)
            draft = self._drafts.get((str(root), page.path))
            self.editor.blockSignals(True)
            self.editor.setPlainText(draft.body if draft else page.body)
            self.editor.blockSignals(False)
            self.location.setText(f"{root}  /  {page.path}.md")
            self.reader.show_markdown(page.body, (root / "wiki" / page.path).parent, anchor=anchor)
            if activate:
                self.tabs.setCurrentIndex(0)
                self.shell.navigate("知识")

        self.io.submit(
            lambda: read_page_context(root, path),
            loaded,
            kb=root,
            obsolete=lambda: root != self.kb or request_id != self._page_request_id,
        )

    def _keep_draft(self):
        if self.kb and self.page:
            key = (str(self.kb), self.page.path)
            previous = self._drafts.get(key)
            if self.editor.toPlainText() == self.page.body:
                self._drafts.pop(key, None)
                return
            self._drafts[key] = PageDraft(
                self.editor.toPlainText(),
                previous.version if previous else self.page.version,
            )

    def _save_page(self):
        if not self.kb or not self.page:
            return
        self._keep_draft()
        key = (str(self.kb), self.page.path)
        draft = self._drafts.get(key, PageDraft(self.page.body, self.page.version))
        body, version = draft.body, draft.version
        task_id = self.manager.submit(self.kb, [SavePage(self.page.path, body, version)])
        self._save_tasks[task_id] = (key, body)
        self.statusBar().showMessage("保存任务已提交；出现版本冲突时草稿会保留。")

    def _review_draft(self):
        if not self.kb or not self.page or self.kb in self._deleting_kbs:
            return
        root, path = self.kb, self.page.path
        request_id = self._page_request_id
        self._keep_draft()

        def obsolete():
            return (
                self.kb != root
                or root in self._deleting_kbs
                or request_id != self._page_request_id
                or not self.page
                or self.page.path != path
            )

        def loaded(latest, error):
            if obsolete() or self._error(error):
                return
            self._keep_draft()
            draft = self._drafts.get(
                (str(root), path), PageDraft(self.page.body, self.page.version)
            )
            dialog = DraftDialog(latest, draft, self)
            if dialog.exec():
                self.page = latest
                self._drafts[(str(root), path)] = PageDraft(
                    dialog.draft.toPlainText(), latest.version
                )
                self.editor.setPlainText(dialog.draft.toPlainText())
                self._save_page()

        self.io.submit(lambda: read_page(root, path), loaded, kb=root, obsolete=obsolete)

    def _export_draft(self):
        if not self.page:
            return
        from openkb.locks import atomic_write_text

        path, _ = QFileDialog.getSaveFileName(self, "导出草稿", "draft.md", "Markdown (*.md)")
        if path:
            destination = Path(path).expanduser().resolve()
            if any((parent / ".openkb/config.yaml").is_file() for parent in destination.parents):
                self._error(ValueError("请导出到知识库之外；知识库页面请通过保存操作更新。"))
                return
            body = self.editor.toPlainText()
            self.io.submit(
                lambda: atomic_write_text(destination, body),
                lambda result, error: self._error(error),
            )

    def _import_files(self):
        if self.kb:
            files, _ = QFileDialog.getOpenFileNames(self, "导入资料")
            if files and not self._quitting:
                self.manager.submit(
                    self.kb, [ImportFile(str(Path(path).resolve())) for path in files]
                )

    def _import_urls(self):
        if self.kb is None:
            return
        value, accepted = QInputDialog.getMultiLineText(
            self, "导入网址", "每行一个 HTTP / HTTPS 地址"
        )
        if accepted and value.strip() and not self._quitting:
            try:
                requests = [ImportUrl(line.strip()) for line in value.splitlines() if line.strip()]
                self.manager.submit(self.kb, requests)
            except ValueError as exc:
                self._error(exc)

    def _import_directory(self):
        if not self.kb:
            return
        path = QFileDialog.getExistingDirectory(self, "递归导入目录")
        if path and not self._quitting:
            root = self.kb
            deletion_version = self._kb_deletion_versions.get(root, 0)

            def obsolete():
                return (
                    self._quitting
                    or root in self._deleting_kbs
                    or deletion_version != (self._kb_deletion_versions.get(root, 0))
                )

            def submit_directory():
                files = sorted(
                    p.resolve()
                    for p in Path(path).rglob("*")
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
                )
                if files and not obsolete():
                    task_id = self.manager.submit(root, [ImportFile(str(path)) for path in files])
                    if obsolete():
                        self.manager.stop(task_id)

            self.io.submit(
                submit_directory,
                lambda _value, error: self._error(error),
                kb=root,
                obsolete=obsolete,
            )

    def _ask(self):
        self.conversations.send()

    def _load_conversation(self, identity):
        self.conversations.open(identity)
        self.shell.navigate("对话")

    def _follow_link(self, url: QUrl):
        if not self.kb:
            return
        if url.scheme() == "openkb":
            self.open_page(url.path(QUrl.ComponentFormattingOption.FullyDecoded), url.fragment())
        elif url.isLocalFile():
            path = Path(url.toLocalFile()).resolve()
            if path.is_relative_to(self.kb / "wiki") and path.suffix == ".md":
                self.open_page(str(path.relative_to(self.kb / "wiki")), url.fragment())
        elif not url.scheme() and url.fragment():
            from openkb.rendering.markdown import heading_anchor

            current = self.chat if self.shell.title.text() == "对话" else self.tabs.currentWidget()
            if isinstance(current, MarkdownView):
                current.scrollToAnchor(heading_anchor(url.fragment()))
        elif url.scheme() in {"http", "https"}:
            QDesktopServices.openUrl(url)

    def _selected_task(self):
        row = self.task_table.currentRow()
        item = self.task_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected_tasks(self):
        return tuple(
            self.task_table.item(row.row(), 0).data(Qt.ItemDataRole.UserRole)
            for row in self.task_table.selectionModel().selectedRows()
        )

    def _retry_selected(self):
        from openkb.desktop.task_actions import RetryDialog

        if task_id := self._selected_task():
            RetryDialog(self, task_id).exec()

    def _clear_selected_history(self):
        from openkb.desktop.task_actions import clear_selected_history

        clear_selected_history(self, self._selected_tasks())

    def _select_task(self):
        task_id = self._selected_task()
        self.artifacts.clear()
        self.workspaces.task_owner.setText("选择任务以查看其所属知识库与保留产物。")
        if task_id:
            try:
                task = self.manager.get(task_id)
            except KeyError:
                return
            self.workspaces.task_owner.setText(
                f"所选任务：{task.id}\n所属知识库：{task.kb_dir}\n保留产物（不代表任务全部成功）"
            )
            for result in task.results:
                self.artifacts.addItems(list(result.resources))

    def _stop_selected(self):
        for task_id in self._selected_tasks():
            try:
                self.manager.stop(task_id)
            except KeyError:
                pass  # A concurrent summary cleanup can remove selected rows.

    def _poll_tasks(self):
        from openkb.desktop.task_progress import progress_presentation, update_task_progress

        tasks = self.manager.tasks()
        running = sum(task.state not in TERMINAL for task in tasks)
        attention = sum(
            task.state in TERMINAL
            and (
                task.state != "completed"
                or any(result.quality or result.unfinished for result in task.results)
            )
            for task in tasks
        )
        self.shell.set_task_status(running, attention)
        self.shell.task_status.setToolTip("查看任务详情、错误、保留产物与安全停止")
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            quality_count = sum(
                bool(result.quality or result.unfinished) for result in task.results
            )
            values = [
                Path(task.kb_dir).name,
                _OPERATIONS.get(task.operation, task.operation),
                _STATES.get(task.state, task.state)
                + (f" · {quality_count} 项质量提示" if quality_count else ""),
                f"成功 {task.succeeded} · 跳过 {task.skipped} · "
                f"失败 {task.failed} · 未处理 {task.unfinished}",
                task.error or _STAGES.get(task.stage, task.stage),
                progress_presentation(task)[1],
            ]
            for column, value in enumerate(values):
                item = self.task_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.task_table.setItem(row, column, item)
                item.setText("" if column == 5 else value)
                item.setData(Qt.ItemDataRole.UserRole, task.id)
                item.setToolTip(task.kb_dir if column == 0 else value)
            update_task_progress(self.task_table, row, task)
            self.conversations.observe(task)
            if task.state in TERMINAL and task.id not in self._seen_terminal:
                self._seen_terminal.add(task.id)
                self._task_finished(task)
        self.task_table.blockSignals(False)
        if self._quitting:
            if self.shutdown_complete():
                self.timer.stop()
                self.tray.hide()
                QApplication.instance().quit()

    def shutdown_complete(self):
        return (
            self.manager.join(0)
            and self.watch_registry.stopped()
            and self.io.stopped()
            and all(view.rendering_stopped() for view in self.findChildren(MarkdownView))
        )

    def _task_finished(self, task):
        if self._quitting:
            self._select_task()
            return
        if task.id in self._save_tasks and task.succeeded:
            key, body = self._save_tasks.pop(task.id)
            draft = self._drafts.get(key)
            version = task.results[-1].revision
            active = self.kb and str(self.kb) == key[0] and self.page and self.page.path == key[1]
            current_body = self.editor.toPlainText() if active else draft.body if draft else body
            confirmed = task.results[-1].page
            if version and (current_body != body or confirmed is None):
                self._drafts[key] = PageDraft(current_body, version)
            else:
                self._drafts.pop(key, None)
            if active and confirmed:
                # The receipt carries exactly the page this save committed.
                # A later filesystem read could silently adopt another writer's
                # unseen version and authorize overwriting it on the next save.
                self.page = confirmed
                self.page_context.invalidate()
                if not self._quitting:
                    self.reader.show_markdown(
                        confirmed.body, (Path(key[0]) / "wiki" / key[1]).parent
                    )
                if current_body == body:
                    self.editor.blockSignals(True)
                    self.editor.setPlainText(confirmed.body)
                    self.editor.blockSignals(False)
        self.conversations.finished(task)
        if task.state != "completed":
            self.statusBar().showMessage("任务未全部完成。点击顶部“任务”查看错误、保留成果与重试。")
        if self.kb and str(self.kb) == task.kb_dir and not self._quitting:
            self._refresh()
            self.workspaces.refresh_inventory(task)
        self._select_task()

    def closeEvent(self, event):
        if self._quitting:
            if self.timer.isActive():
                event.ignore()
                self.statusBar().showMessage("正在退出；可查看任务进度与结果，或安全停止所选任务。")
            else:
                event.accept()
            return
        event.ignore()
        if QSystemTrayIcon.isSystemTrayAvailable() and self.tray.isVisible():
            self.hide()
        else:
            self.statusBar().showMessage("系统托盘不可用，窗口保持可见。请使用“退出”结束程序。")

    def request_quit(self):
        if self._quitting:
            return
        self.watch_registry.stop_all(close=False)
        stop = False
        if any(task.state not in TERMINAL for task in self.manager.tasks()):
            box = QMessageBox(self)
            box.setWindowTitle("退出 UrltraKB")
            box.setText("后台仍有任务。选择等待完成，或在安全边界停止。已完成成果会保留。")
            wait = box.addButton("等待完成并退出", QMessageBox.ButtonRole.AcceptRole)
            halt = box.addButton("安全停止并退出", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() not in (wait, halt):
                self.statusBar().showMessage("已取消退出。目录监听已停止，可手动重新启用。")
                return
            stop = box.clickedButton() == halt
        self._quitting = True
        self.watch_registry.stop_all()
        from openkb.desktop.layout import observe_shutdown

        self.manager.shutdown(stop=stop)
        self.io.stop()
        observe_shutdown(self)
        for view in self.findChildren(MarkdownView):
            view.stop_rendering()
        self.statusBar().showMessage("正在收尾并回收后台进程…")
