"""Native progress presentation against controlled, non-executing task snapshots."""

from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QProgressBar


def verify_progress(window, kb, root, wait):
    from openkb.desktop.task_details import show_task_details
    from openkb.progress import ProgressStep
    from openkb.runtime.records import TaskView
    from openkb.runtime.tasks import _Task

    task = TaskView(
        "a" * 32,
        str(kb),
        "ReparseSource",
        "running",
        "parsing",
        1,
        (),
        False,
        False,
        progress=(
            ProgressStep("docx", 37, 100, "paragraphs"),
            ProgressStep("pdf", 3, 8, "pages"),
            ProgressStep("cloud_ocr"),
        ),
    )
    unknown = replace(task, id="b" * 32, progress=(ProgressStep("cloud_ocr"),))
    partial = replace(task, id="c" * 32, state="partial", processes_reaped=True)
    snapshots = [task, unknown, partial]
    assert not window.manager.tasks()
    try:
        with window.manager._condition:
            window.manager._tasks.update({t.id: _Task(t) for t in snapshots})
        window._seen_terminal.update(t.id for t in snapshots)
        window.shell.navigate("任务")
        window.resize(1366, 768)
        window._poll_tasks()
        wait(lambda: window.task_table.rowCount() == 3)
        bar = window.task_table.cellWidget(0, 5).bar
        assert isinstance(bar, QProgressBar) and bar.value() == 37
        assert "37/100" in bar.toolTip() and "3/8" in bar.toolTip()
        assert window.task_table.cellWidget(1, 5).bar.maximum() == 0
        assert "等待云端" in window.task_table.cellWidget(1, 5).label.text()
        assert "部分完成" in window.task_table.cellWidget(2, 5).bar.format()
        for theme, name in (("浅色", "light"), ("深色", "dark")):
            window.theme.setCurrentText(theme)
            QApplication.processEvents()
            window._poll_tasks()
            QApplication.processEvents()
            cell = window.task_table.cellWidget(0, 5)
            assert cell.label.height() >= cell.label.sizeHint().height(), (
                cell.label.height(),
                cell.label.sizeHint().height(),
                cell.height(),
                window.task_table.rowHeight(0),
            )
            assert window.task_table.item(0, 5).text() == ""
            window.grab().save(str(root / f"task-progress-{name}.png"))
        observed = []

        def update():
            with window.manager._condition:
                window.manager._tasks[task.id].view = replace(
                    task, progress=(ProgressStep("docx", 58, 100, "paragraphs"),)
                )

        def inspect():
            dialog = QApplication.activeModalWidget()
            try:
                progress = dialog.findChild(QProgressBar, "task-detail-progress")
                observed.append(progress.value() == 58 and "58/100" in progress.toolTip())
                dialog.grab().save(str(root / "task-progress-details.png"))
            finally:
                dialog.accept()

        QTimer.singleShot(100, update)
        QTimer.singleShot(1300, inspect)
        show_task_details(task, window, manager=window.manager)
        assert observed == [True], observed
    finally:
        with window.manager._condition:
            window.manager._tasks.clear()
