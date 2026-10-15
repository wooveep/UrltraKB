"""Display identity and bundled artwork for UltraKB."""

from pathlib import Path

from PySide6.QtGui import QIcon

NAME = "UltraKB"
ASSETS = Path(__file__).parent / "assets/brand"


def application_icon():
    return QIcon(str(ASSETS / "ultrakb-app-icon.svg"))


def mark_icon():
    return QIcon(str(ASSETS / "ultrakb-mark.svg"))
