"""Native workbench: KB navigation, reading/editing, questions, and task observation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSystemTrayIcon,
)

from ultrakb.application.catalog import knowledge_bases
from ultrakb.application.pages import Page, read_page
from ultrakb.application.reading import read_knowledge_context, read_page_context
from ultrakb.config import GLOBAL_CONFIG_DIR
from ultrakb.desktop.brand import NAME
from ultrakb.desktop.editor import DraftDialog, PageDraft
from ultrakb.desktop.io import LocalIO
from ultrakb.desktop.reader import MarkdownView
from ultrakb.page_ops import EDITABLE_SECTIONS
from ultrakb.runtime.records import TERMINAL
from ultrakb.runtime.requests import (
    SavePage,
)
from ultrakb.runtime.tasks import TaskManager
from ultrakb.runtime.watch import NativeWatchRegistry


class Workbench(QMainWindow):
    def __init__(self, *, history_dir: Path | None = None, first_use: bool = False):
        super().__init__()
        self.setWindowTitle(NAME)
        self.resize(1320, 900)
        self.kb: Path | None = None
        self.view_id: str | None = None
        self._requested_page: str | None = None
        self.page: Page | None = None
        self._knowledge_scope = None
        self._knowledge_request_id = 0
        self._drafts: dict[tuple[str, str, str], PageDraft] = {}
        self._save_tasks: dict[str, tuple[tuple[str, str], str]] = {}
        self._seen_terminal: set[str] = set()
        self._deleting_kbs: set[Path] = set()
        self._kb_deletion_versions: dict[Path, int] = {}
        self._page_request_id = 0
        self._open_request_id = 0
        self._pending_open: Path | None = None
        self._chat_task: str | None = None
        self._quitting = False
        self._first_use = first_use
        self._creation_dialog = None
        self.io = LocalIO(self)
        self.control_io = LocalIO(self)
        self.manager = TaskManager(
            history_dir=history_dir or GLOBAL_CONFIG_DIR / "desktop/tasks",
            require_verified_connection=True,
        )
        self.watch_registry = NativeWatchRegistry(self.manager)
        from ultrakb.desktop.pending_dispatcher import PendingDispatcher

        self.pending_dispatcher = PendingDispatcher(self)
        from ultrakb.desktop.layout import build_workbench

        build_workbench(self)
        from ultrakb.desktop.onboarding import Onboarding

        self.onboarding = Onboarding(self)
        self._build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_tasks)
        self.timer.start(150)
        self.io.submit(knowledge_bases, self._recent_loaded, global_settings=True)
        self.reader.show_markdown(
            "# UltraKB\n\n打开已有知识库，或在您选择的位置创建一个。\n\n"
            "资料、页面、对话和任务始终归属于各自的知识库。",
            Path.cwd(),
        )

    @property
    def scope(self):
        from ultrakb.knowledge_scope import live_scope

        return live_scope(self.kb, self.view_id) if self.kb and self.view_id else None

    @property
    def read_scope(self):
        return self._knowledge_scope

    def _watch(self):
        self.shell.navigate("任务")
        self.workspaces.task_tabs.setCurrentIndex(1)

    def _about(self):
        from ultrakb.desktop.about import AboutDialog

        AboutDialog(self.io, self).exec()

    def _settings(self, *, global_defaults=False):
        self.shell.navigate("设置")
        self.workspaces.settings_tabs.setCurrentIndex(0 if global_defaults else 1)

    def _documents(self):
        self.shell.navigate("资料")

    def _knowledge_bases(self):
        from ultrakb.desktop.knowledge_bases import KnowledgeBasesDialog

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
            from ultrakb.desktop.task_details import show_task_details

            try:
                task = self.manager.get(task_id)
            except KeyError:
                return
            show_task_details(task, self)

    def _maintenance(self):
        if self.kb is not None:
            from ultrakb.desktop.maintenance import MaintenanceDialog

            MaintenanceDialog(self, self.kb).exec()

    def _manage_artifacts(self):
        self.shell.navigate("产物")

    def _diagnose(self, path=None):
        from ultrakb.desktop.diagnostics import DiagnosticsDialog

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
        from ultrakb.desktop.layout import build_tray

        build_tray(self)

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
            if self._first_use and not values and not self.kb and not self._pending_open:
                self._first_use = False
                self._create_kb()

    def _choose_kb(self):
        path = QFileDialog.getExistingDirectory(self, "打开知识库")
        if path:
            self.open_knowledge_base(Path(path))

    def _create_kb(self):
        from ultrakb.desktop.setup_wizard import SetupWizard

        if self._quitting:
            return
        if self._creation_dialog:
            self._creation_dialog.raise_()
            self._creation_dialog.activateWindow()
            return
        dialog = self._creation_dialog = SetupWizard(self, creating=True)

        def finished(_):
            self._creation_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()

    def open_knowledge_base(self, path: Path, *, workspace=None):
        path = path.expanduser().resolve()
        if path in self._deleting_kbs:
            return
        self._open_request_id += 1
        request_id = self._open_request_id
        self._pending_open = path

        def loaded(value, error):
            if request_id != self._open_request_id:
                return
            if error:
                self._pending_open = None
                from ultrakb.desktop.publication_baseline import offer_baseline

                if offer_baseline(self, path, error, request_id, workspace=workspace):
                    return
                self._opened(None, error, attempted=path)
                return

            def current():
                from ultrakb.lifecycle import current_generation
                from ultrakb.mutation import RecoveryRequired

                try:
                    return (
                        request_id == self._open_request_id
                        and path not in self._deleting_kbs
                        and current_generation(path) == value.scope.publication.generation
                    )
                except (OSError, ValueError, RecoveryRequired):
                    return False

            def ready():
                self._pending_open = None
                self._opened(
                    value.scope.kb_dir, None, attempted=path, knowledge=value, rendered=True
                )
                if self.kb == path and workspace is not None:
                    self.shell.navigate(workspace)

            if value.context is None:
                ready()
            else:
                page = value.context.page
                self.reader.show_markdown(
                    page.body,
                    (value.scope.wiki_dir / page.path).parent,
                    preserve=True,
                    on_ready=ready,
                    is_current=current,
                )

        if self.kb:
            self.kbs.setCurrentIndex(self.kbs.findData(str(self.kb)))
        self.statusBar().showMessage(f"正在打开 {path} 的已公开知识…")
        self.io.submit(
            lambda: read_knowledge_context(path),
            loaded,
            kb=path,
            snapshot=True,
            obsolete=lambda: request_id != self._open_request_id,
        )

    def _opened(self, root, error, *, attempted=None, knowledge=None, rendered=False):
        from ultrakb.mutation import RecoveryRequired

        if isinstance(error, RecoveryRequired) and attempted is not None:
            self._diagnose(attempted)
            return
        if self._error(error):
            return
        self._keep_draft()
        self.kb, self.page = root, None
        self._knowledge_scope = knowledge.scope if knowledge else None
        self.pending_dispatcher.watch(root)
        self.view_id = None
        self.page_context.clear()
        self._page_request_id += 1
        self._chat_task = None
        if knowledge is None:
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
        if knowledge is not None:
            from ultrakb.desktop.knowledge_loading import (
                apply_knowledge,
                load_details,
                load_sessions,
            )

            request_id = self._open_request_id

            def obsolete():
                return (
                    self.kb != root
                    or root in self._deleting_kbs
                    or request_id != self._open_request_id
                )

            apply_knowledge(
                self,
                knowledge,
                on_ready=lambda: load_details(self, knowledge.scope, obsolete),
                rendered=rendered,
            )
            load_sessions(self, root, self.view_id, obsolete)
            from ultrakb.config import register_kb

            self.io.submit(
                lambda: register_kb(root),
                lambda value, error: self._error(error),
                kb=root,
                global_settings=True,
                obsolete=obsolete,
            )
        else:
            self._refresh()
        self.conversations.recover(root)
        self.onboarding.switched(root)

    def _refresh(self):
        from ultrakb.desktop.knowledge_loading import refresh

        refresh(self)

    def _refresh_current(self):
        self._refresh()

    def _activate_page(self, item, column):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self.open_page(path)

    def open_page(self, path: str, anchor: str = "", *, activate=True):
        if self.kb is None or self.kb in self._deleting_kbs:
            return
        root = self.kb
        scope = self.read_scope
        self._requested_page = path.removesuffix(".md")
        self._page_request_id += 1
        request_id = self._page_request_id
        self._keep_draft()
        if not getattr(self, "_page_loading", False):
            self._page_restore_location = self.location.text()
        self._page_loading = activate
        self._page_loading_root = root
        target = self._requested_page
        if activate:
            self.shell.navigate("知识")
            self.tabs.setCurrentIndex(0)
            self.location.setText(f"正在读取「{Path(target).name}」…")
            self.save_button.setEnabled(False)
            self.workspaces.ask_page.setEnabled(False)

        def loaded(context, error):
            if self.kb != root or request_id != self._page_request_id:
                return
            if error:
                self._page_loading = False
                self._requested_page = None
                self.save_button.setEnabled(False)
                self.workspaces.ask_page.setEnabled(False)
                self.location.setText(f"「{Path(target).name}」读取失败，点击标题可重试。")
                if self.page is None:
                    self.reader.show_temporary("读取失败。请点击知识目录中的标题重试。")
                self._error(error)
                return
            from ultrakb.desktop.knowledge_loading import display_context

            display_context(self, context, scope, anchor=anchor)

        if scope is None:
            self._refresh()
            return

        self.io.submit(
            lambda: read_page_context(root, path, scope=scope),
            loaded,
            kb=root,
            snapshot=True,
            obsolete=lambda: root != self.kb or request_id != self._page_request_id,
            on_wait=lambda: self.location.setText(
                f"正在等待知识库完成当前写入，随后打开「{Path(target).name}」…"
            )
            if activate
            else None,
        )

    def _cancel_page_read(self):
        self._page_request_id += 1
        self._page_loading = False
        self._requested_page = None
        if self.page and self.kb == self._page_loading_root:
            self.workspaces.select_page(self.page.path)
            self.location.setText(self._page_restore_location)
            self.reader.show_markdown(
                self.page.body,
                (
                    (self.read_scope.wiki_dir if self.read_scope else self.kb / "wiki")
                    / self.page.path
                ).parent,
            )
            self.save_button.setEnabled(self.page.path.split("/")[0] in EDITABLE_SECTIONS)
            self.workspaces.ask_page.setEnabled(True)
        else:
            self.location.setText("尚未选择知识页面")
            self.reader.show_temporary("请从知识目录选择一个页面。")

    def _keep_draft(self):
        if self.kb and self.page:
            key = (str(self.kb), self.view_id or "legacy", self.page.path)
            previous = self._drafts.get(key)
            if self.editor.toPlainText() == self.page.body:
                self._drafts.pop(key, None)
                return
            self._drafts[key] = PageDraft(
                self.editor.toPlainText(),
                previous.version if previous else self.page.version,
                previous.publication_id if previous else self.page.publication_id,
            )

    def _save_page(self):
        if not self.kb or not self.page:
            return
        self._keep_draft()
        key = (str(self.kb), self.view_id or "legacy", self.page.path)
        draft = self._drafts.get(
            key, PageDraft(self.page.body, self.page.version, self.page.publication_id)
        )
        body, version = draft.body, draft.version
        task_id = self.manager.submit(
            self.kb,
            [
                SavePage(
                    self.page.path,
                    body,
                    version,
                    view_id=self.view_id,
                    publication_id=draft.publication_id,
                )
            ],
        )
        self._save_tasks[task_id] = (key, body)
        self.statusBar().showMessage("保存任务已提交；出现版本冲突时草稿会保留。")

    def _review_draft(self):
        if not self.kb or not self.page or self.kb in self._deleting_kbs:
            return
        root, path = self.kb, self.page.path
        view_id = self.view_id
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
                (str(root), view_id or "legacy", path), PageDraft(self.page.body, self.page.version)
            )
            dialog = DraftDialog(latest, draft, self)
            if dialog.exec():
                self.page = latest
                self._drafts[(str(root), view_id or "legacy", path)] = PageDraft(
                    dialog.draft.toPlainText(), latest.version, latest.publication_id
                )
                self.editor.setPlainText(dialog.draft.toPlainText())
                self._save_page()

        def read_latest():
            from ultrakb.application.views import published_scope

            return read_page(root, path, scope=published_scope(root, view_id or "legacy"))

        self.io.submit(read_latest, loaded, kb=root, snapshot=True, obsolete=obsolete)

    def _export_draft(self):
        if not self.page:
            return
        from ultrakb.locks import atomic_write_text

        path, _ = QFileDialog.getSaveFileName(self, "导出草稿", "draft.md", "Markdown (*.md)")
        if path:
            destination = Path(path).expanduser().resolve()
            if any((parent / ".ultrakb/config.yaml").is_file() for parent in destination.parents):
                self._error(ValueError("请导出到知识库之外；知识库页面请通过保存操作更新。"))
                return
            body = self.editor.toPlainText()
            self.io.submit(
                lambda: atomic_write_text(destination, body),
                lambda result, error: self._error(error),
            )

    def _import_files(self):
        from ultrakb.desktop.import_actions import import_files

        import_files(self)

    def _import_urls(self):
        from ultrakb.desktop.import_actions import import_urls

        import_urls(self)

    def _import_directory(self):
        from ultrakb.desktop.import_actions import import_directory

        import_directory(self)

    def _ask(self):
        self.conversations.send()

    def _load_conversation(self, identity):
        self.conversations.open(identity)
        self.shell.navigate("对话")

    def _follow_link(self, url: QUrl):
        if not self.kb:
            return
        if url.scheme() == "ultrakb":
            self.open_page(url.path(QUrl.ComponentFormattingOption.FullyDecoded), url.fragment())
        elif url.isLocalFile():
            path = Path(url.toLocalFile()).resolve()
            wiki = self.read_scope.wiki_dir if self.read_scope else self.kb / "wiki"
            if path.is_relative_to(wiki) and path.suffix == ".md":
                self.open_page(str(path.relative_to(wiki)), url.fragment())
        elif not url.scheme() and url.fragment():
            from ultrakb.rendering.markdown import heading_anchor

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
        if checked := self.workspaces.task_workspace.checked:
            return tuple(checked)
        return tuple(
            self.task_table.item(row.row(), 0).data(Qt.ItemDataRole.UserRole)
            for row in self.task_table.selectionModel().selectedRows()
        )

    def _retry_selected(self):
        from ultrakb.desktop.task_actions import RetryDialog

        if task_ids := self._selected_tasks():
            RetryDialog(self, task_ids).exec()

    def _clear_selected_history(self):
        from ultrakb.desktop.task_actions import clear_selected_history

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
                f"所属知识库：{task.kb_dir}"
                if any(result.resources for result in task.results)
                else (
                    f"当前任务所属知识库：{Path(task.kb_dir).name}。"
                    "详情中可查看完整位置与逐项结果。"
                )
            )
            self.workspaces.task_owner.setToolTip(f"任务：{task.id}\n所属知识库：{task.kb_dir}")
            for result in task.results:
                self.artifacts.addItems(list(result.resources))
        self.artifacts.setVisible(self.artifacts.count() > 0)

    def _stop_selected(self):
        for task_id in self._selected_tasks():
            try:
                self.request_task_stop(task_id)
            except KeyError:
                pass  # A concurrent summary cleanup can remove selected rows.

    def _schedule_task_control(self, operation):
        self.control_io.submit(operation, lambda value, error: self._error(error))

    def request_task_stop(self, task_id):
        self.manager.stop(task_id, schedule_control=self._schedule_task_control)

    def _poll_tasks(self):
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
        self.workspaces.task_workspace.refresh(tasks)
        for task in tasks:
            self.conversations.observe(task)
            self.workspaces.import_progress.observe(task)
            if task.state in TERMINAL and task.id not in self._seen_terminal:
                self._seen_terminal.add(task.id)
                self._task_finished(task)
        if self._quitting:
            if self.shutdown_complete():
                self.timer.stop()
                self.tray.hide()
                self.control_io.stop()
                QApplication.instance().quit()

    def shutdown_complete(self):
        return (
            self.manager.join(0)
            and self.watch_registry.stopped()
            and self.io.stopped()
            and self.control_io.stopped()
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
            active = (
                self.kb
                and str(self.kb) == key[0]
                and self.page
                and (self.view_id or "legacy") == key[1]
                and self.page.path == key[2]
            )
            current_body = self.editor.toPlainText() if active else draft.body if draft else body
            confirmed = task.results[-1].page
            if version and (current_body != body or confirmed is None):
                self._drafts[key] = PageDraft(
                    current_body, version, confirmed.publication_id if confirmed else None
                )
            else:
                self._drafts.pop(key, None)
            if active and confirmed:
                # Retain the opened save baseline while a complete public
                # context is prepared; the displayed old context stays coherent.
                self._drafts[key] = PageDraft(
                    current_body, confirmed.version, confirmed.publication_id
                )
                if current_body == body:
                    self.editor.blockSignals(True)
                    self.editor.setPlainText(confirmed.body)
                    self.editor.blockSignals(False)
        self.conversations.finished(task)
        self.workspaces.import_progress.refresh_available(Path(task.kb_dir))
        if task.state != "completed":
            self.statusBar().showMessage("任务未全部完成。点击顶部“任务”查看错误、保留成果与重试。")
        if self.kb and str(self.kb) == task.kb_dir and not self._quitting:
            self.conversations.refresh_focus()
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
            box.setWindowTitle("退出 UltraKB")
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
        from ultrakb.desktop.layout import observe_shutdown

        self.manager.shutdown(stop=stop, schedule_control=self._schedule_task_control)
        self.io.stop()
        observe_shutdown(self)
        for view in self.findChildren(MarkdownView):
            view.stop_rendering()
        self.statusBar().showMessage("正在收尾并回收后台进程…")
