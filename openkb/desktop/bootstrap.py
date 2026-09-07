"""Native entry point, including frozen spawn support before any Qt import."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def main() -> int:
    multiprocessing.freeze_support()
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QApplication

    from openkb.desktop.window import Workbench

    app = QApplication(sys.argv)
    app.setApplicationName("OpenKB")
    app.setOrganizationName("OpenKB")
    app.setQuitOnLastWindowClosed(False)
    fonts = Path(__file__).parent.parent / "rendering/assets/fonts"
    for path in fonts.glob("*.otf"):
        QFontDatabase.addApplicationFont(str(path))
    window = Workbench()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
