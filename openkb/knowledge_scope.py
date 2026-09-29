"""Separate knowledge paths from the real KB's configuration and write lease."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from openkb.source_records import ViewId


@dataclass(frozen=True)
class KnowledgeScope:
    kb_dir: Path
    wiki_dir: Path
    view_id: str = "legacy"

    def __post_init__(self) -> None:
        root = self.kb_dir.expanduser().resolve()
        wiki = self.wiki_dir.expanduser().resolve()
        TypeAdapter(ViewId).validate_python(self.view_id)
        if not wiki.is_relative_to(root):
            raise ValueError("Invalid page path: wiki escapes its knowledge base")
        relative = wiki.relative_to(root).parts
        managed = (
            (
                len(relative) == 4
                and relative[:2] in {(".openkb", "staging"), (".openkb", "proposals")}
                and relative[-1] == "wiki"
            )
            or (
                len(relative) == 6
                and relative[:3] == (".openkb", "knowledge", self.view_id)
                and relative[3] == "revisions"
                and relative[-1] == "wiki"
            )
            or (
                relative == (".openkb", "knowledge", self.view_id, "wiki")
                and self.view_id != "legacy"
            )
        )
        if not managed and not (self.view_id == "legacy" and wiki == root / "wiki"):
            raise ValueError("Invalid knowledge view directory")
        object.__setattr__(self, "kb_dir", root)
        object.__setattr__(self, "wiki_dir", wiki)

    @property
    def read_only(self) -> bool:
        relative = self.wiki_dir.relative_to(self.kb_dir).parts
        return (len(relative) == 6 and relative[3] == "revisions") or (
            len(relative) > 1 and relative[1] == "proposals"
        )


def legacy_scope(kb_dir: Path) -> KnowledgeScope:
    root = kb_dir.expanduser().resolve()
    return KnowledgeScope(root, root / "wiki")


def live_scope(kb_dir: Path, view_id: str = "legacy") -> KnowledgeScope:
    root = kb_dir.expanduser().resolve()
    wiki = root / "wiki" if view_id == "legacy" else root / ".openkb/knowledge" / view_id / "wiki"
    return KnowledgeScope(root, wiki, view_id)


def resolve_scope(
    kb_dir: Path, scope: KnowledgeScope | None = None, *, writable: bool = False
) -> KnowledgeScope:
    """Adapt old callers once; scoped calls must belong to the same real KB."""
    selected = scope if scope is not None else legacy_scope(kb_dir)
    if selected.kb_dir != kb_dir.expanduser().resolve():
        raise ValueError("Knowledge scope belongs to a different knowledge base")
    if writable and selected.read_only:
        raise ValueError("Knowledge revisions and proposals are read-only")
    return selected
