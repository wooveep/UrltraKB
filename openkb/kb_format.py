"""Supported knowledge-base format; creation is explicit and never a migration."""

import json
from pathlib import Path

KB_FORMAT = {"format": "openkb.context-kb", "version": 1}


def require_current_kb(root: Path) -> None:
    marker = root / ".openkb/format.json"
    try:
        value = json.loads(marker.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Unsupported knowledge-base format; create a new knowledge base") from exc
    if marker.is_symlink() or value != KB_FORMAT or type(value.get("version")) is not int:
        raise ValueError("Unsupported knowledge-base format; original data was left unchanged")


def validate_existing_format(root: Path) -> None:
    # Low-level locks also guard creation/recovery and plain scratch directories.
    # An existing configured KB must declare its format before lock-file creation
    # or mutation recovery can alter any of its own files.
    if (root / ".openkb/config.yaml").exists() or (root / ".openkb/format.json").exists():
        require_current_kb(root)
