"""Private Linux font supply with the pinned U+002B fallback correction."""

from pathlib import Path
from xml.sax.saxutils import escape

FONT_SUBSTITUTION_POLICY = "private-office-fonts-v2:plus-u002b"
_BOLD_SHA = "11c2d7c3dee34f6195ca89ed359d90c7e56385bb5cd41934e1f56eb586db1f8b"
_LICENSE_SHA = "e905e9c8d4a717ba8d97aab6ee42306fe1de5907536e09f489f6507f912cc541"
FONT_FILES = {
    "share/fonts/truetype/FrankRuhlHofshi-Bold.otf": _BOLD_SHA,
    "openkb-provenance/licenses/OfficeSymbol-OFL.txt": _LICENSE_SHA,
}


def require_font_supply(manifest) -> None:
    if any(manifest.files.get(name) != checksum for name, checksum in FONT_FILES.items()):
        raise ValueError(
            "Private Office runtime needs the pinned Office U+002B font correction; "
            "rebuild it with scripts/prepare_office_runtime.py"
        )


def build_fontconfig(fonts: Path, cache: Path) -> str:
    """Use only vetted runtime fonts, without changing body fallback metrics."""
    return (
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">'
        "<fontconfig><dir>"
        + escape(str(fonts.resolve()))
        + "</dir><cachedir>"
        + escape(str(cache.resolve()))
        + "</cachedir>"
        + "</fontconfig>"
    )
