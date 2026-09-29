"""Separate knowledge paths from the real KB's configuration and write lease."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KnowledgeScope:
    kb_dir: Path
    wiki_dir: Path
    view_id: str = "legacy"

    def __post_init__(self) -> None:
        root = self.kb_dir.expanduser().resolve()
        wiki = self.wiki_dir.expanduser().resolve()
        if not wiki.is_relative_to(root):
            raise ValueError("Invalid page path: wiki escapes its knowledge base")
        if self.view_id != "legacy" or wiki != root / "wiki":
            raise ValueError("Only the knowledge base's legacy scope is available")
        object.__setattr__(self, "kb_dir", root)
        object.__setattr__(self, "wiki_dir", wiki)


def legacy_scope(kb_dir: Path) -> KnowledgeScope:
    root = kb_dir.expanduser().resolve()
    return KnowledgeScope(root, root / "wiki")


def resolve_scope(kb_dir: Path, scope: KnowledgeScope | None = None) -> KnowledgeScope:
    """Adapt old callers once; scoped calls must belong to the same real KB."""
    selected = scope if scope is not None else legacy_scope(kb_dir)
    if selected.kb_dir != kb_dir.expanduser().resolve():
        raise ValueError("Knowledge scope belongs to a different knowledge base")
    return selected
