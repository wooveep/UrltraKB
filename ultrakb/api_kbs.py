"""KB discovery for ``GET /api/v1/kbs`` (the web UI's KB switcher).

Split out of :mod:`ultrakb.api_helpers` so that module stays under the per-file
line gate (``tests/test_file_size.py``). Discovery unions the children of
``kb_root_dir()`` with the KBs registered in global.yaml (``kb_aliases`` /
``known_kbs``), so a KB created outside the root — the CLI initializes a KB
wherever it is run — is still surfaced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ultrakb.api_helpers import _is_kb_dir
from ultrakb.config import kb_root_dir, registered_kbs


def _kb_list_item(kb_dir: Path, name: str) -> dict[str, Any]:
    """Read rollup counters from the committed main-store publication."""
    from ultrakb.application.knowledge_bases import get_kb_status

    status = get_kb_status(kb_dir)
    return {
        "name": name,
        "path": str(kb_dir),
        "document_count": status["total_indexed"],
        "last_compile": status["last_compile"],
        "has_raw": (kb_dir / "raw").is_dir(),
    }


def _list_knowledge_bases() -> dict[str, Any]:
    """List knowledge bases for the web UI's KB switcher.

    Discovery unions two sources and de-dupes by RESOLVED path so a KB that is
    both a root child and registered appears exactly once:

    1. children of ``kb_root_dir()`` that look like a KB (``_is_kb_dir``); and
    2. KBs registered in global.yaml (``kb_aliases`` name→path, then
       ``known_kbs`` paths) that live OUTSIDE the root.

    Root children are visited first, so a dual-source KB keeps its directory
    name (which equals the alias for KBs created via ``/init``); a registry-only
    KB keeps its alias name (or, for a bare ``known_kbs`` path, its directory
    name). ``registered_kbs()`` already yields collision-resolved UNIQUE names
    (a bare ``known_kbs`` entry colliding with a root child's basename is hidden
    there), so names here are unique too; the by-name guard below is a
    belt-and-suspenders backstop. Stale/missing registry entries (fail
    ``_is_kb_dir``) are skipped. The response still carries the top-level
    ``root``.
    """
    root = kb_root_dir()
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    items: list[dict[str, Any]] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not _is_kb_dir(child):
                continue
            resolved = child.resolve()
            seen_paths.add(str(resolved))
            seen_names.add(child.name)
            items.append(_kb_list_item(resolved, child.name))
    for name, kb_dir in registered_kbs():
        if str(kb_dir) in seen_paths or name in seen_names or not _is_kb_dir(kb_dir):
            continue
        seen_paths.add(str(kb_dir))
        seen_names.add(name)
        items.append(_kb_list_item(kb_dir, name))
    return {"root": str(root), "knowledge_bases": items}
