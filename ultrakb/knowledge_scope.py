"""Separate knowledge paths from the real KB's configuration and write lease."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from ultrakb.source_records import ViewId

if TYPE_CHECKING:
    from ultrakb.locks import PrivateMaterialization
    from ultrakb.published_knowledge import PublishedKnowledge
    from ultrakb.storage.knowledge_store import BoundKnowledge


@dataclass(frozen=True)
class KnowledgeScope:
    kb_dir: Path
    wiki_dir: Path
    view_id: str = "legacy"
    publication: "PublishedKnowledge | None" = field(default=None, repr=False)
    knowledge: "BoundKnowledge | None" = field(default=None, repr=False)
    private_workspace: "PrivateMaterialization | None" = field(default=None, repr=False)

    def __post_init__(self) -> None:
        root = self.kb_dir.expanduser().resolve()
        if self.private_workspace is not None:
            from ultrakb.locks import PrivateMaterialization

            if (
                type(self.private_workspace) is not PrivateMaterialization
                or self.publication is not None
                or self.knowledge is not None
                or not self.private_workspace.permits_scope(self.wiki_dir)
            ):
                raise ValueError("Compiler view requires its active private materialization")
            TypeAdapter(ViewId).validate_python(self.view_id)
            object.__setattr__(self, "kb_dir", root)
            return
        if self.knowledge is not None:
            TypeAdapter(ViewId).validate_python(self.view_id)
            if (
                self.publication is not None
                or self.knowledge.root != root
                or self.knowledge.publication.view_id != self.view_id
                or self.wiki_dir != root / "wiki"
            ):
                raise ValueError("Public reading context belongs to another knowledge snapshot")
            object.__setattr__(self, "kb_dir", root)
            return  # The logical base must not follow a mutable worktree symlink.
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
        if self.knowledge is not None:
            return self.knowledge.publication_id
        return self.publication.publication_id if self.publication is not None else None

    @cached_property
    def reading_view(self):
        if self.knowledge is None:
            raise ValueError("Managed reading requires an explicitly bound publication")
        from ultrakb.application.storage_selection import query_view

        return query_view(self)

    @property
    def historical(self) -> bool:
        return self.read_only and self.publication is None and self.knowledge is None

    @property
    def read_only(self) -> bool:
        if self.private_workspace is not None:
            return False
        if self.knowledge is not None:
            return True
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
    if selected.private_workspace is not None and not selected.private_workspace.permits_scope(
        selected.wiki_dir
    ):
        raise ValueError("Compiler private materialization has finished")
    if writable and selected.read_only:
        raise ValueError("Knowledge revisions and proposals are read-only")
    return selected


@contextmanager
def reading_scope(scope: KnowledgeScope, *, cancelled=None):
    """Only an explicitly fixed public snapshot can bypass the writer's lease."""
    if scope.knowledge is not None:
        from ultrakb.lifecycle import read_lifecycle

        with read_lifecycle(
            scope.kb_dir, database_identity=scope.knowledge.identity, cancelled=cancelled
        ):
            yield
    elif scope.publication is None:
        from ultrakb.locks import kb_read_lock

        with kb_read_lock(scope.kb_dir / ".ultrakb", cancelled=cancelled):
            yield
    else:
        from ultrakb.lifecycle import expected_generation, read_lifecycle

        with (
            expected_generation(scope.kb_dir, scope.publication.generation),
            read_lifecycle(scope.kb_dir, cancelled=cancelled),
        ):
            yield


def read_wiki_bytes(scope: KnowledgeScope, path: str) -> bytes:
    """Resolve an exact logical resource through its explicitly selected authority."""
    from ultrakb.storage.wiki_paths import wiki_path

    wiki_path(path)
    if scope.knowledge is not None:
        if path in scope.reading_view.native_inputs:
            unit, original = scope.reading_view.native_inputs[path]
            return scope.knowledge.read_source(unit, original)
        return scope.knowledge.read(path)
    target = (scope.wiki_dir / path).resolve()
    if not target.is_relative_to(scope.wiki_dir):
        raise ValueError("Invalid page path")
    return target.read_bytes()


def wiki_page_paths(scope: KnowledgeScope) -> tuple[str, ...]:
    """Enumerate safe Markdown names without hashing unrelated binary assets."""
    if scope.knowledge is not None:
        return tuple(sorted(path for path in scope.reading_view.files if path.endswith(".md")))
    return tuple(
        sorted(
            path.relative_to(scope.wiki_dir).as_posix()
            for path in scope.wiki_dir.rglob("*.md")
            if path.is_file() and path.resolve().is_relative_to(scope.wiki_dir)
        )
    )
