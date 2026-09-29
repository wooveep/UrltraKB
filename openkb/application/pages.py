"""Read committed pages and save drafts with explicit version preconditions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb import frontmatter
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.locks import kb_ingest_lock, kb_read_lock
from openkb.page_ops import edit_wiki_page, validate_page_ref


@dataclass(frozen=True)
class Page:
    path: str
    content: str
    body: str
    version: str


@dataclass(frozen=True)
class PageSave:
    status: Literal["saved", "conflict", "not_found"]
    page: Page | None
    draft: str
    ghosts_stripped: tuple[str, ...] = ()


def read_page(kb_dir: Path, path: str, *, scope: KnowledgeScope | None = None) -> Page:
    kb_dir = kb_dir.expanduser().resolve()
    if not (kb_dir / ".openkb").is_dir():
        raise FileNotFoundError(f"Knowledge base not found: {kb_dir}")
    with kb_read_lock(kb_dir / ".openkb"):
        wiki = resolve_scope(kb_dir, scope).wiki_dir
        rel = path if path.endswith(".md") else f"{path}.md"
        target = (wiki / rel).resolve()
        if not wiki.is_relative_to(kb_dir) or not target.is_relative_to(wiki):
            raise ValueError("Invalid page path.")
        content = target.read_text(encoding="utf-8")
    parts = frontmatter.split(content)
    return Page(
        target.relative_to(wiki).with_suffix("").as_posix(),
        content,
        parts[1] if parts else content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def save_page(
    kb_dir: Path,
    path: str,
    body: str,
    *,
    version: str | None = None,
    scope: KnowledgeScope | None = None,
) -> PageSave:
    """Keep metadata and normalize links; a stale desktop draft is never overwritten.

    Legacy callers may omit the precondition, preserving the existing edit API.
    Desktop callers bind each save to the version returned when opening a page.
    """
    section, stem = validate_page_ref(path)
    scope = resolve_scope(kb_dir, scope)
    path = f"{section}/{stem}"
    if not (kb_dir / ".openkb").is_dir():
        raise FileNotFoundError(f"Knowledge base not found: {kb_dir}")
    with kb_ingest_lock(kb_dir / ".openkb"):
        try:
            current = read_page(kb_dir, f"{path}.md", scope=scope)
        except FileNotFoundError:
            return PageSave("not_found", None, body)
        if version is not None and version != current.version:
            return PageSave("conflict", current, body)
        result = edit_wiki_page(kb_dir, path, body, scope=scope)
        return PageSave(
            "saved",
            read_page(kb_dir, f"{path}.md", scope=scope),
            body,
            tuple(result["ghosts_stripped"]),
        )
