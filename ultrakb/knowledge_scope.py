"""Separate knowledge paths from the real KB's configuration and write lease."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from ultrakb.source_records import ViewId

if TYPE_CHECKING:
    from ultrakb.published_knowledge import PublishedKnowledge


@dataclass(frozen=True)
class KnowledgeScope:
    kb_dir: Path
    wiki_dir: Path
    view_id: str = "legacy"
    publication: "PublishedKnowledge | None" = field(default=None, repr=False)

    def __post_init__(self) -> None:
        root = self.kb_dir.expanduser().resolve()
        wiki = self.wiki_dir.expanduser().resolve()
        TypeAdapter(ViewId).validate_python(self.view_id)
        if not wiki.is_relative_to(root):
            raise ValueError("Invalid page path: wiki escapes its knowledge base")
        relative = wiki.relative_to(root).parts
        managed = (
            len(relative) == 4
            and relative[:2] in {(".ultrakb", "staging"), (".ultrakb", "proposals")}
            and relative[-1] == "wiki"
        ) or (
            len(relative) == 6
            and relative[:3] == (".ultrakb", "knowledge", self.view_id)
            and relative[3] == "revisions"
            and relative[-1] == "wiki"
        )
        if not managed and not (self.view_id == "legacy" and wiki == root / "wiki"):
            raise ValueError("Invalid knowledge view directory")
        object.__setattr__(self, "kb_dir", root)
        object.__setattr__(self, "wiki_dir", wiki)
        if self.publication is not None and (
            self.publication.kb_dir != root
            or self.publication.wiki_dir != wiki
            or self.publication.view_id != self.view_id
        ):
            raise ValueError("Public reading context belongs to another knowledge snapshot")

    @property
    def publication_id(self) -> str | None:
        return self.publication.publication_id if self.publication is not None else None

    @property
    def historical(self) -> bool:
        return self.read_only and self.publication is None

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
    return KnowledgeScope(root, root / "wiki", view_id)


def resolve_scope(
    kb_dir: Path, scope: KnowledgeScope | None = None, *, writable: bool = False
) -> KnowledgeScope:
    """Bind live or historical knowledge to the same real KB."""
    selected = scope if scope is not None else legacy_scope(kb_dir)
    if selected.kb_dir != kb_dir.expanduser().resolve():
        raise ValueError("Knowledge scope belongs to a different knowledge base")
    if writable and selected.read_only:
        raise ValueError("Knowledge revisions and proposals are read-only")
    return selected


@contextmanager
def reading_scope(scope: KnowledgeScope):
    """Only an explicitly fixed public snapshot can bypass the writer's lease."""
    if scope.publication is None:
        from ultrakb.locks import kb_read_lock

        with kb_read_lock(scope.kb_dir / ".ultrakb"):
            yield
    else:
        from ultrakb.lifecycle import expected_generation, read_lifecycle

        with (
            expected_generation(scope.kb_dir, scope.publication.generation),
            read_lifecycle(scope.kb_dir),
        ):
            yield
