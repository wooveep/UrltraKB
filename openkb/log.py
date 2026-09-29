"""Append-only operation log for the wiki (log.md)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.locks import atomic_write_text, kb_ingest_lock


def append_log(
    wiki_dir: Path, operation: str, description: str, *, scope: KnowledgeScope | None = None
) -> None:
    """Append an entry to wiki/log.md.

    Format: ``## [YYYY-MM-DD HH:MM:SS] operation | description``
    """
    root = wiki_dir.parent
    if scope is not None:
        scope = resolve_scope(scope.kb_dir, scope)
        if wiki_dir.resolve() != scope.wiki_dir:
            raise ValueError("Operation log belongs to a different knowledge scope")
        if scope.read_only:
            return
        root = scope.kb_dir
    log_path = wiki_dir / "log.md"
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"## [{date_str}] {operation} | {description}\n\n"

    with kb_ingest_lock(root / ".openkb"):
        content = (
            log_path.read_text(encoding="utf-8") if log_path.exists() else "# Operations Log\n\n"
        )
        atomic_write_text(log_path, content + entry)
