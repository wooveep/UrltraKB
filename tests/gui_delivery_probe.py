"""Native interaction assertions in a dedicated QApplication; no model execution."""

import argparse
import json
import logging
import os
import time
from dataclasses import replace
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario", choices=("navigation", "documents", "progress", "tasks", "task_batches")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CONFIG_HOME"] = str(root / "profile")
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QAbstractButton, QDialog, QPlainTextEdit

    from ultrakb import config
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop.source_reader import SourceReader
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.runtime.records import TaskView, UnitResult
    from ultrakb.state import HashRegistry

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    kb = root / "交互验收库"
    initialize_kb(kb, seed_environment=False)
    with kb_ingest_lock(kb / ".ultrakb"):
        for name in ("第一知识", "第二知识"):
            atomic_write_text(kb / f"wiki/concepts/{name}.md", f"# {name}\n\n{name}正文。")
        atomic_write_text(kb / "wiki/sources/原文.md", "# 原文\n\n可读资料正文。")
        HashRegistry(kb / ".ultrakb/hashes.json").add(
            "a" * 64, {"name": "可读资料.md", "doc_name": "原文", "type": "md"}
        )
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    logging.disable(logging.CRITICAL)
    window = Workbench(history_dir=root / "tasks")
    window.resize(1366, 768)
    window.show()
    restorations = []

    def pump(ms=100):
        deadline = time.monotonic() + ms / 1000
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

    def wait(condition):
        deadline = time.monotonic() + 15
        while not condition():
            pump(10)
            assert time.monotonic() < deadline, "Native interaction timed out"
        pump(30)

    def click(widget, pos=None):
        point = widget.mapTo(window, pos or widget.rect().center())
        target = window.childAt(point)
        assert target is not None
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=target.mapFrom(window, point))
        pump(100)

    def node():
        return next(
            item
            for item in window.pages.findItems(
                "*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive
            )
            if item.data(0, Qt.ItemDataRole.UserRole) == "concepts/第二知识.md"
        )

    def patch(obj, attr, value):
        restorations.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        if args.scenario == "navigation":
            window.open_page("concepts/第一知识")
            wait(lambda: window.page and window.page.path == "concepts/第一知识")
            window.pages.expandAll()
            target = node()
            window.pages.scrollToItem(target)
            with kb_ingest_lock(kb / ".ultrakb"):
                click(window.pages.viewport(), window.pages.visualItemRect(target).center())
                pump(350)
                assert "等待" in window.location.text(), window.location.text()
                assert "第二知识" in window.location.text()
                assert "第一知识正文" not in window.reader.toPlainText()
                window.shell.navigate("资料")
            wait(lambda: not window.io._callbacks)
            assert window.shell.title.text() == "资料", "Late page read stole navigation"
            window.shell.navigate("知识")
            wait(lambda: "第一知识正文" in window.reader.toPlainText())
            assert "正在读取" not in window.reader.toPlainText()
            assert (
                window.pages.currentItem().data(0, Qt.ItemDataRole.UserRole)
                == "concepts/第一知识.md"
            )
            assert window.workspaces.ask_page.isEnabled()
            target = node()
            click(window.pages.viewport(), window.pages.visualItemRect(target).center())
            wait(lambda: window.page.path == "concepts/第二知识")
            # A failed reread of the already selected title must be retryable too.
            from PySide6.QtWidgets import QApplication, QMessageBox

            from ultrakb.desktop import window as window_module

            original_read = window_module.read_page_context
            calls, dismissed = [], []

            def fail_once(*args, **kwargs):
                calls.append(args[1])
                if len(calls) == 1:
                    raise OSError("fixture local read failure")
                return original_read(*args, **kwargs)

            def dismiss():
                box = QApplication.activeModalWidget()
                if isinstance(box, QMessageBox):
                    dismissed.append(True)
                    box.accept()
                else:
                    QTimer.singleShot(10, dismiss)

            patch(window_module, "read_page_context", fail_once)
            QTimer.singleShot(10, dismiss)
            window.open_page("concepts/第二知识")
            wait(lambda: bool(dismissed) and not window.io._callbacks)
            target = node()
            click(window.pages.viewport(), window.pages.visualItemRect(target).center())
            wait(lambda: len(calls) == 2 and "第二知识正文" in window.reader.toPlainText())
            window.grab().save(str(root / "knowledge.png"))
        elif args.scenario == "documents":
            window.shell.navigate("资料")
            panel = window.workspaces.panels["资料"][0]
            wait(lambda: panel.table.rowCount() == 1)
            importer, options = button(window, "导入资料"), button(window, "导入选项")
            assert importer.mapTo(window, QPoint()).y() == options.mapTo(window, QPoint()).y()
            assert window.shell.subtitle.width() >= window.shell.subtitle.sizeHint().width()
            for theme in ("浅色", "深色"):
                window.theme.setCurrentText(theme)
                for width, height in ((1366, 768), (720, 600)):
                    window.resize(width, height)
                    pump()
                    for control in panel.table.cellWidget(0, 3).findChildren(QAbstractButton):
                        assert control.visibleRegion().contains(control.rect()), control.text()
                    window.grab().save(str(root / f"documents-{theme}-{width}.png"))
            window.resize(1366, 768)
            pump()
            for label in ("阅读", "提问"):
                window.shell.navigate("资料")
                control = next(
                    b
                    for b in panel.table.cellWidget(0, 3).findChildren(QAbstractButton)
                    if b.text() == label
                )
                with kb_ingest_lock(kb / ".ultrakb"):
                    click(control, QPoint(control.width() // 2, control.height() - 3))
                    pump(350)
                    assert "等待" in panel.status.text(), panel.status.text()
                if label == "阅读":
                    wait(lambda: any(r.isVisible() for r in window.findChildren(SourceReader)))
                    next(r for r in window.findChildren(SourceReader) if r.isVisible()).close()
                else:
                    wait(lambda: window.question.isVisible())
            assert not window.manager.tasks()
        else:
            current = [
                TaskView(
                    "f" * 32, str(kb), "ImportFile", "running", "converting", 3, (), False, False
                )
            ]
            patch(window.manager, "tasks", lambda: tuple(current))
            patch(
                window.manager, "get", lambda identity: next(t for t in current if t.id == identity)
            )
            if args.scenario == "progress":
                window.shell.navigate("资料")
                panel = window.workspaces.panels["资料"][0]
                wait(lambda: panel.table.rowCount() == 1)
                progress = window.workspaces.import_progress
                progress.track(kb, current[0].id)
                pump(350)
                before = progress.status.text()
                with kb_ingest_lock(kb / ".ultrakb"):
                    atomic_write_text(kb / "wiki/sources/第二份.md", "# 新资料\n\n已保留。")
                    HashRegistry(kb / ".ultrakb/hashes.json").add(
                        "b" * 64, {"name": "第二份.md", "doc_name": "第二份", "type": "md"}
                    )
                current[0] = replace(current[0], results=(UnitResult("completed"),))
                pump(750)
                assert progress.status.text() != before, "Completed count did not refresh"
                assert "1 / 3" in progress.status.text()
                assert "converting" not in progress.status.text()
                wait(lambda: panel.table.rowCount() == 2)
                observed = []
                QTimer.singleShot(
                    100, lambda: current.__setitem__(0, replace(current[0], stage="compiling"))
                )

                def inspect():
                    dialog = next(
                        d
                        for d in window.findChildren(QDialog)
                        if d.isVisible() and d.windowTitle() == "任务结果"
                    )
                    observed.append(dialog.findChild(QPlainTextEdit).toPlainText())
                    dialog.grab().save(str(root / "task-details.png"))
                    dialog.accept()

                QTimer.singleShot(700, inspect)
                progress.show_details()
                assert "编译" in observed[0] and "转换" not in observed[0]
                window.grab().save(str(root / "progress.png"))
            elif args.scenario == "tasks":
                current.extend(
                    [
                        replace(
                            current[0],
                            id="e" * 32,
                            state="completed",
                            stage="committed",
                            total=1,
                            results=(UnitResult("completed"),),
                            processes_reaped=True,
                        ),
                        replace(
                            current[0],
                            id="d" * 32,
                            state="failed",
                            stage="failed",
                            total=1,
                            results=(UnitResult("failed"),),
                            processes_reaped=True,
                        ),
                    ]
                )
                stopped = []
                patch(window.manager, "stop", stopped.append)
                window.shell.navigate("任务")
                pump(250)
                tasks = window.workspaces.task_workspace
                table = window.task_table

                def item(identity):
                    return next(
                        table.item(i, 0)
                        for i in range(table.rowCount())
                        if table.item(i, 0).data(Qt.ItemDataRole.UserRole) == identity
                    )

                item("f" * 32).setCheckState(Qt.CheckState.Checked)
                table.setCurrentItem(item("e" * 32))
                assert tasks.checked == {"f" * 32}, "Focus changed checkbox intent"
                current[0] = replace(current[0], stage="compiling")
                pump(250)
                assert tasks.checked == {"f" * 32}
                tasks.select_visible()
                assert len(tasks.checked) == 3
                tasks.stop_checked()
                assert stopped == ["f" * 32], stopped
                assert "1" in tasks.status.text() and "2" in tasks.status.text()
                tasks.state_filter.setCurrentText("已结束")
                assert not tasks.checked and table.rowCount() == 2
                tasks.search.setFocus()
                tasks.search.setText("交互")
                QTest.keyClick(tasks.search, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
                assert tasks.search.selectedText() == "交互" and not tasks.checked
                table.setFocus()
                QTest.keyClick(table, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
                assert tasks.checked == {"e" * 32, "d" * 32}
                for theme in ("浅色", "深色"):
                    window.theme.setCurrentText(theme)
                    for width, height in ((1366, 768), (720, 600)):
                        window.resize(width, height)
                        pump()
                        for row in range(table.rowCount()):
                            controls = table.cellWidget(row, 5)
                            for control in controls.findChildren(QAbstractButton):
                                assert control.visibleRegion().contains(control.rect())
                        window.grab().save(str(root / f"tasks-{theme}-{width}.png"))
            else:
                task_batches(window, kb, root, current, patch, pump, wait)
    finally:
        for obj, attr, original in reversed(restorations):
            setattr(obj, attr, original)
        window.workspaces.import_progress.task_id = None
        window.request_quit()
        wait(window.shutdown_complete)
    print(json.dumps({"scenario": args.scenario, "passed": True, "model_sends": 0}))


def task_batches(window, kb, root, current, patch, pump, wait):
    """Exercise reviewed UI batches at the scheduler boundary; core retry has worker tests."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop import task_actions
    from ultrakb.runtime.records import UnitResult
    from ultrakb.runtime.requests import ImportFile, SavePage
    from ultrakb.runtime.task_actions import RetryPreview

    other = root / "另一知识库"
    initialize_kb(other, seed_environment=False)
    current.extend(
        [
            replace(
                current[0],
                id="e" * 32,
                state="completed",
                stage="committed",
                total=1,
                results=(UnitResult("completed"),),
                processes_reaped=True,
            ),
            replace(
                current[0],
                id="d" * 32,
                kb_dir=str(other),
                state="partial",
                stage="failed",
                total=2,
                results=(UnitResult("completed"), UnitResult("failed")),
                processes_reaped=True,
            ),
        ]
    )
    retried, connections, cleared, confirmations = [], [], [], []

    def inputs(identity):
        view = next(task for task in current if task.id == identity)
        if view.state == "running":
            raise ValueError("请等待任务结束并完成进程回收")
        return view, (SavePage("index", "body", "version"), ImportFile(str(root / "input.md"))), ()

    def preview(_manager, identity):
        view, _requests, _identities = inputs(identity)
        return RetryPreview(
            identity,
            view.kb_dir,
            view.operation,
            "reviewed",
            (1,),
            (),
            (),
            "所有单元已完成" if identity == "e" * 32 else None,
        )

    def retry(_manager, reviewed):
        retried.append((reviewed.task_id, reviewed.indices))
        return "c" * 32

    original_ensure = window.onboarding.ensure
    window.onboarding.ready[other] = True

    def ensure(target, description, continuation, *, valid, background=False):
        connections.append(target)
        assert valid() and background
        original_ensure(target, description, continuation, valid=valid, background=background)

    def clear(identities):
        cleared.append(tuple(identities))
        current[:] = [task for task in current if task.id not in identities]

    patch(window.manager, "retry_inputs", inputs)
    patch(window.manager, "clear_history", clear)
    patch(window.onboarding, "ensure", ensure)
    patch(task_actions, "preview_retry", preview)
    patch(task_actions, "retry_task", retry)
    window.shell.navigate("任务")
    pump(200)
    dialog = task_actions.RetryDialog(window, tuple(task.id for task in current))
    dialog.show()
    wait(lambda: dialog.check_button.isEnabled())
    assert set(dialog.previews) == {"d" * 32}
    assert set(dialog.reasons) == {"e" * 32, "f" * 32}
    assert "选中 3 项" in dialog.details.toPlainText()
    dialog.retry_button.click()
    dialog.retry_button.click()
    wait(lambda: len(retried) == 1 and dialog.check_button.isEnabled())
    assert retried == [("d" * 32, (1,))], retried
    assert connections == [other]
    dialog.grab().save(str(root / "batch-retry.png"))
    dialog.reject()

    def confirm():
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            confirmations.append(box.text())
            box.done(QMessageBox.StandardButton.Yes)
        else:
            QTimer.singleShot(20, confirm)

    QTimer.singleShot(20, confirm)
    task_actions.clear_selected_history(window, tuple(task.id for task in current))
    wait(lambda: bool(cleared))
    assert set(cleared[0]) == {"e" * 32, "d" * 32}
    assert len(confirmations) == 1 and "仅清理 2 项" in confirmations[0]
    wait(lambda: window.task_table.rowCount() == 1)
    assert window.manager.get("f" * 32).state == "running"


if __name__ == "__main__":
    main()
