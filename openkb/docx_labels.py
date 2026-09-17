"""Read literal filenames drawn in attachment icons; never execute or OCR them."""

from __future__ import annotations

import re
import struct


def icon_filename(data: bytes) -> str | None:
    # MS-EMF 2.3.5.8 / 2.2.5: EMR_EXTTEXTOUTW and its counted Unicode string.
    # https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-emf/a59a79ac-328e-492d-a34d-e02727af6edf
    if (
        len(data) < 88
        or len(data) > 4_000_000
        or struct.unpack_from("<I", data)[0] != 1
        or data[40:44] != b" EMF"
        or struct.unpack_from("<I", data, 48)[0] != len(data)
    ):
        return None
    position = 0
    fragments = ""
    names = set()
    while position < len(data):
        if position + 8 > len(data):
            return None
        kind, size = struct.unpack_from("<II", data, position)
        if size < 8 or size % 4 or position + size > len(data):
            return None
        if kind == 0x54:
            if size < 56:
                return None
            count, offset, options = struct.unpack_from("<III", data, position + 44)
            if not options & 0x10:  # Glyph indices are not Unicode text.
                if count > 1024 or offset < 56 or offset % 2 or offset + count * 2 > size:
                    return None
                try:
                    text = data[position + offset : position + offset + count * 2].decode(
                        "utf-16-le"
                    )
                except UnicodeError:
                    return None
                fragments += text
                if len(fragments) > 1024:
                    return None
                if re.fullmatch(r"[^\\/:\x00-\x1f]+\.[A-Za-z0-9]{1,10}", fragments.strip()):
                    names.add(fragments.strip())
                    fragments = ""
        position += size
    return names.pop() if len(names) == 1 and not fragments.strip() else None
