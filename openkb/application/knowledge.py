"""A user-facing knowledge catalog, separate from the wiki's storage tree."""

from dataclasses import dataclass
from pathlib import Path

from openkb import frontmatter
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.locks import kb_read_lock
from openkb.schema import PAGE_CONTENT_DIRS

KNOWLEDGE_LABELS = {
    "summaries": "资料摘要",
    "concepts": "主题与概念",
    "entities": "人物与事物",
    "explorations": "探索笔记",
}


@dataclass(frozen=True)
class KnowledgeEntry:
    path: str
    title: str
    section: str
    description: str
    modified: float


def page_title(content: str, fallback: str) -> str:
    metadata = frontmatter.parse(content) or {}
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    parts = frontmatter.split(content)
    body = parts[1] if parts else content
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def list_knowledge(
    kb_dir: Path, *, scope: KnowledgeScope | None = None
) -> tuple[KnowledgeEntry, ...]:
    scope = resolve_scope(kb_dir, scope)
    root = kb_dir.resolve()
    wiki = scope.wiki_dir
    entries = []
    with kb_read_lock(root / ".openkb"):
        for section in (*PAGE_CONTENT_DIRS, "explorations"):
            for path in (wiki / section).rglob("*.md"):
                if not path.resolve().is_relative_to(wiki) or any(
                    part.startswith(".") for part in path.relative_to(wiki).parts
                ):
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                    metadata = frontmatter.parse(content) or {}
                    description = metadata.get("description", "")
                    entries.append(
                        KnowledgeEntry(
                            path.relative_to(wiki).as_posix(),
                            page_title(content, path.stem),
                            section,
                            description if isinstance(description, str) else "",
                            path.stat().st_mtime,
                        )
                    )
                except (OSError, UnicodeError):
                    continue
    return tuple(sorted(entries, key=lambda entry: (-entry.modified, entry.path)))
