"""Local PageIndex storage cleanup, without constructing an LLM client.

The exactly pinned PageIndex dependency owns this version-1 SQLite layout.
All callers hold the KB write lease and journal these database/blob paths.
Connections close before the caller can restore a failed filesystem mutation.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from openkb.locks import kb_ingest_lock_held


def remove_index_document(kb_dir: Path, doc_name: str, doc_id: str | None) -> tuple[bool, str]:
    root = kb_dir / ".openkb"
    if not kb_ingest_lock_held(root):
        raise RuntimeError("Local index cleanup requires the KB write lease")
    database = root / "pageindex.db"
    if not database.exists():
        return False, "no PageIndex state"
    # mode=rw prevents a typo or concurrent external deletion from creating an
    # empty database and masquerading as a successful cleanup.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        column, value = ("doc_id", doc_id) if doc_id else ("doc_name", doc_name)
        rows = db.execute(
            f"SELECT doc_id, file_path FROM documents WHERE collection_name = ? AND {column} = ?",
            ("default", value),
        ).fetchall()
        if not rows:
            return False, "no PageIndex doc to delete"
        if len(rows) > 1:
            raise ValueError(
                f"{len(rows)} PageIndex docs match doc_name='{doc_name}'; "
                "skipping (re-add to refresh)",
            )
        identity, file_path = rows[0]
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            raise ValueError("Invalid local index document identity")
        owned = (root / "files/default").resolve()
        if not owned.is_relative_to(root.resolve()) or owned == root.resolve():
            raise ValueError("Local index managed directory escapes the knowledge base")
        blob = Path(file_path).resolve() if file_path else None
        images = (owned / identity).resolve()
        if not images.is_relative_to(owned) or (
            blob is not None and (not blob.is_relative_to(owned) or blob == owned)
        ):
            raise ValueError("Local index path escapes its managed files")
        if blob is not None:
            blob.unlink(missing_ok=True)
        if images.exists():
            shutil.rmtree(images)
        with db:
            db.execute(
                "DELETE FROM documents WHERE collection_name = ? AND doc_id = ?",
                ("default", identity),
            )
    return True, f"deleted PageIndex doc ({identity[:12]}…)"
