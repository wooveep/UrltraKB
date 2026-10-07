"""Record OS fonts visible to the native Windows and macOS Office backends."""

import os
import sys
from pathlib import Path

from openkb.office.inventory import digest


def font_supply() -> dict:
    if sys.platform == "darwin":
        fonts = {}
        for directory in (
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library/Fonts",
        ):
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".otf", ".ttf", ".ttc", ".dfont"}:
                    fonts[str(path)] = {"sha256": digest(path)}
        return {
            "policy": "private-office-fonts-plus-recorded-macos-fonts-v1",
            "system_fonts": fonts,
        }
    if sys.platform != "win32":
        from openkb.office.fontconfig import FONT_FILES, FONT_SUBSTITUTION_POLICY

        return {
            "policy": FONT_SUBSTITUTION_POLICY,
            "glyph_correction": {
                "family": "Frank Ruhl Hofshi",
                "weight": 700,
                "codepoint": "U+002B",
            },
            "applies_to": "linux",
            "font_files": "pinned-runtime-manifest",
            "required_files": FONT_FILES,
        }
    import winreg

    windows = Path(os.environ["SystemRoot"]) / "Fonts"
    user = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        / "Microsoft/Windows/Fonts"
    )
    fonts = {}
    key_name = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive, basis in ((winreg.HKEY_LOCAL_MACHINE, windows), (winreg.HKEY_CURRENT_USER, user)):
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    family, value, kind = winreg.EnumValue(key, index)
                    if kind not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
                        raise ValueError("Unsupported registered Windows font path")
                    path = Path(os.path.expandvars(value))
                    path = path if path.is_absolute() else basis / path
                    fonts[str(path)] = {"family": family, "sha256": digest(path)}
        except FileNotFoundError:
            continue
    return {"policy": "private-office-fonts-plus-recorded-windows-fonts-v1", "system_fonts": fonts}
