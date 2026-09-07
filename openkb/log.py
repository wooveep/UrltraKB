"""Append-only operation log for the wiki (log.md)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openkb.locks import atomic_write_text


def append_log(wiki_dir: Path, operation: str, description: str) -> None:
    """Append an entry to wiki/log.md.

    Format: ``## [YYYY-MM-DD HH:MM:SS] operation | description``
    """
    log_path = wiki_dir / "log.md"
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"## [{date_str}] {operation} | {description}\n\n"

    content = log_path.read_text(encoding="utf-8") if log_path.exists() else "# Operations Log\n\n"
    atomic_write_text(log_path, content + entry)
