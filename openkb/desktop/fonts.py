"""Application-local typography from the exact font files supplied with UrltraKB."""

import json
import os
import sys
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

SANS = "Source Han Sans CN"
MONO = "Source Code Pro"


def register_fonts():
    bundled = Path(__file__).parent / "assets/fonts"
    root = bundled if bundled.is_dir() else Path(__file__).parents[2] / "assets/fonts"
    for face in json.loads((root / "manifest.json").read_text("utf-8")):
        filename, family = face["file"], face["family"]
        identity = QFontDatabase.addApplicationFont(str(root / filename))
        if identity < 0 or family not in QFontDatabase.applicationFontFamilies(identity):
            raise RuntimeError(f"Bundled application font could not be loaded: {filename}")


def text_font(size=15, *, code=False):
    font = QFont()
    font.setFamilies([MONO, SANS] if code else [SANS])
    # Leave style and axes unset so QSS/Markdown can select the bundled real
    # Regular, Medium, Bold and italic faces, including Chinese code fallback.
    font.setWeight(QFont.Weight.Normal)
    font.setPixelSize(size)
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def application_arguments(arguments):
    """Use Qt's bundled FreeType rasterizer on Windows; respect explicit Qt overrides."""
    arguments = list(arguments) or ["UrltraKB"]
    if (
        sys.platform == "win32"
        and not os.environ.get("QT_QPA_PLATFORM")
        and not any(arg == "-platform" or arg.startswith("-platform=") for arg in arguments)
    ):
        arguments.extend(["-platform", "windows:fontengine=freetype"])
    return arguments
