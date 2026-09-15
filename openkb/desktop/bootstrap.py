"""Native entry point, including frozen spawn support before any Qt import."""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()
    from PySide6.QtWidgets import QApplication, QMessageBox

    from openkb.desktop.fonts import application_arguments
    from openkb.desktop.instance import DesktopInstance

    app = QApplication(application_arguments(sys.argv))
    from openkb.desktop.brand import NAME, application_icon

    app.setApplicationName("OpenKB")
    app.setApplicationDisplayName(NAME)
    app.setWindowIcon(application_icon())
    app.setOrganizationName("OpenKB")
    app.setQuitOnLastWindowClosed(False)
    instance = None
    try:
        instance = DesktopInstance(app)
        if not instance.start():
            return 0
        # Claim ownership before importing/creating any task manager or tray.
        from openkb.desktop.window import Workbench

        window = Workbench()
        instance.bind(window._show_window)
        window.show()
        return app.exec()
    except (OSError, RuntimeError) as error:
        QMessageBox.warning(None, NAME, str(error))
        return 1
    finally:
        if instance is not None:
            instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
