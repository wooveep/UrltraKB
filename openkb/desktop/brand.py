"""Display identity; compatibility names and storage paths remain OpenKB/openkb."""

from pathlib import Path

from PySide6.QtGui import QIcon

NAME = "UrltraKB"
ASSETS = Path(__file__).parent / "assets/brand"


def application_icon():
    return QIcon(str(ASSETS / "openkb-app-icon.svg"))


def mark_icon():
    return QIcon(str(ASSETS / "openkb-mark.svg"))
