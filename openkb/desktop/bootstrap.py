"""Native entry point, including frozen spawn support before any Qt import."""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()
    from PySide6.QtWidgets import QApplication

    from openkb.desktop.fonts import application_arguments
    from openkb.desktop.window import Workbench

    app = QApplication(application_arguments(sys.argv))
    from openkb.desktop.brand import NAME, application_icon

    app.setApplicationName("OpenKB")
    app.setApplicationDisplayName(NAME)
    app.setWindowIcon(application_icon())
    app.setOrganizationName("OpenKB")
    app.setQuitOnLastWindowClosed(False)
    window = Workbench()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
