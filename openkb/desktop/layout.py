"""Assemble the native shell and retain an observable shutdown workspace."""

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog

from openkb.desktop.shell import WorkbenchShell
from openkb.desktop.workspaces import Workspaces


def build_workbench(window):
    preferences = QSettings(
        QSettings.defaultFormat(), QSettings.Scope.UserScope, "OpenKB", "OpenKB"
    )
    window.shell = WorkbenchShell(window, preferences)
    window.setCentralWidget(window.shell)
    window.workspaces = Workspaces(window)
    from openkb.desktop.appearance import Appearance

    window.appearance = Appearance(window, preferences)
    window.shutdown_controls = (
        window.shell.top,
        window.shell.navigation,
        window.workspaces.retry,
        window.workspaces.clear,
        window.workspaces.watches,
    )


def observe_shutdown(window):
    """Progress, diagnostics and cooperative stop stay available after business input ends."""
    for dialog in window.findChildren(QDialog):
        if dialog.isWindow() and dialog.isVisible():
            dialog.done(QDialog.DialogCode.Rejected)
    window.shell.navigate("任务")
    window.workspaces.task_tabs.setCurrentIndex(0)
    for index in range(window.shell.stack.count()):
        if window.shell.stack.widget(index) is not window.shell.stack.currentWidget():
            window.shell.stack.widget(index).setEnabled(False)
    for control in window.shutdown_controls:
        control.setEnabled(False)
    window._show_window()
