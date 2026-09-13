"""Conservative separation of copy labels from meaningful document identity."""

import re
from pathlib import PurePath


def document_label(name):
    """Remove explicit copy decorations; retain subject, version and unknown qualifiers."""
    stem = PurePath(name).stem
    stem = re.sub(r"^copy of\s+", "", stem, flags=re.I)
    stem = re.sub(
        r"(?:[\s_-]+(?:copy|副本)(?:[\s_-]+\d+)?|"
        r"\s*[（(\[]\s*(?:copy|副本)(?:\s+\d+)?\s*[）)\]])$",
        "",
        stem,
        flags=re.I,
    ).strip()
    return stem or PurePath(name).stem
