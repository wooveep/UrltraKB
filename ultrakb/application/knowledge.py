"""A user-facing knowledge catalog, separate from the wiki's storage tree."""

from dataclasses import dataclass
from pathlib import Path

from ultrakb import frontmatter
from ultrakb.knowledge_scope import (
    KnowledgeScope,
    read_wiki_bytes,
    reading_scope,
    resolve_scope,
    wiki_page_paths,
)
from ultrakb.schema import PAGE_CONTENT_DIRS

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
    wiki = scope.wiki_dir
    entries = []
    with reading_scope(scope):
        if scope.knowledge is not None:
            from datetime import datetime

            from ultrakb.storage.knowledge_store import KnowledgeStore

            receipt = KnowledgeStore(scope.kb_dir, scope.knowledge.identity).receipt(
                scope.knowledge.publication_id
            )
            published_at = datetime.fromisoformat(receipt.published_at).timestamp()
            for logical in wiki_page_paths(scope):
                section = logical.split("/", 1)[0]
                if section not in (*PAGE_CONTENT_DIRS, "explorations") or not logical.endswith(
                    ".md"
                ):
                    continue
                if any(part.startswith(".") for part in logical.split("/")):
                    continue
                content = read_wiki_bytes(scope, logical).decode("utf-8")
                metadata = frontmatter.parse(content) or {}
                description = metadata.get("description", "")
                entries.append(
                    KnowledgeEntry(
                        logical,
                        page_title(content, Path(logical).stem),
                        section,
                        description if isinstance(description, str) else "",
                        published_at,
                    )
                )
            return tuple(sorted(entries, key=lambda entry: entry.path))
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
