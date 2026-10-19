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
    from ultrakb.storage.knowledge_store import BoundKnowledge


@dataclass(frozen=True)
class KnowledgeScope:
    kb_dir: Path
    wiki_dir: Path
    view_id: str = "legacy"
    knowledge: "BoundKnowledge | None" = field(default=None, repr=False)
    private_workspace: "PrivateMaterialization | None" = field(default=None, repr=False)
    _historical: bool = field(default=False, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        root = self.kb_dir.expanduser().resolve()
        if self.private_workspace is not None:
            from ultrakb.locks import PrivateMaterialization

            if (
                type(self.private_workspace) is not PrivateMaterialization
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
                self.knowledge.root != root
                or self.knowledge.publication.view_id != self.view_id
                or self.wiki_dir != root / "wiki"
            ):
                raise ValueError("Public reading context belongs to another knowledge snapshot")
            object.__setattr__(self, "kb_dir", root)
            return  # The logical base must not follow a mutable worktree symlink.
        TypeAdapter(ViewId).validate_python(self.view_id)
        if self.wiki_dir.expanduser().absolute() != root / "wiki":
            raise ValueError("Only a main-store snapshot or active compiler workspace is supported")
        object.__setattr__(self, "kb_dir", root)

    @property
    def publication_id(self) -> str | None:
        return self.knowledge.publication_id if self.knowledge is not None else None

    @cached_property
    def reading_view(self):
        if self.knowledge is None:
            raise ValueError("Managed reading requires an explicitly bound publication")
        from ultrakb.application.storage_selection import query_view

        return query_view(self)

    @property
    def historical(self) -> bool:
        return self._historical

    @property
    def read_only(self) -> bool:
        return self.private_workspace is None


def legacy_scope(kb_dir: Path) -> KnowledgeScope:
    return live_scope(kb_dir)


def live_scope(kb_dir: Path, view_id: str = "legacy") -> KnowledgeScope:
    from ultrakb.application.views import view_scope

    return view_scope(kb_dir, view_id)


def resolve_scope(
    kb_dir: Path, scope: KnowledgeScope | None = None, *, writable: bool = False
) -> KnowledgeScope:
    """Bind live or historical knowledge to the same real KB."""
    selected = scope if scope is not None else live_scope(kb_dir)
    if selected.kb_dir != kb_dir.expanduser().resolve():
        raise ValueError("Knowledge scope belongs to a different knowledge base")
    if selected.knowledge is None and selected.private_workspace is None:
        if selected.wiki_dir != selected.kb_dir / "wiki":
            raise ValueError("Only a main-store snapshot or active compiler workspace is supported")
        selected = live_scope(kb_dir, selected.view_id)
    if selected.private_workspace is not None and not selected.private_workspace.permits_scope(
        selected.wiki_dir
    ):
        raise ValueError("Compiler private materialization has finished")
    if writable and (selected.historical or (selected.read_only and selected.knowledge is None)):
        raise ValueError("Knowledge revisions and proposals are read-only")
    return selected


def resolve_private_scope(kb_dir: Path, scope: KnowledgeScope | None) -> KnowledgeScope:
    """File compilers may modify only their owned disposable materialization."""
    selected = resolve_scope(kb_dir, scope, writable=True)
    if selected.private_workspace is None:
        raise ValueError("Compilation requires an active private materialization")
    return selected


@contextmanager
def reading_scope(scope: KnowledgeScope, *, cancelled=None):
    """Protect the fixed database generation; private materializations own their files."""
    if scope.knowledge is not None:
        from ultrakb.lifecycle import read_lifecycle

        with read_lifecycle(
            scope.kb_dir, database_identity=scope.knowledge.identity, cancelled=cancelled
        ):
            yield
    elif scope.private_workspace is not None and scope.private_workspace.permits_scope(
        scope.wiki_dir
    ):
        yield
    else:
        raise ValueError("Reading requires a bound main-store snapshot")


def read_wiki_bytes(scope: KnowledgeScope, path: str) -> bytes:
    """Resolve an exact logical resource through its explicitly selected authority."""
    from ultrakb.storage.wiki_paths import wiki_path

    wiki_path(path)
    if scope.knowledge is not None:
        if path in scope.reading_view.native_inputs:
            unit, original = scope.reading_view.native_inputs[path]
            return scope.knowledge.read_source(unit, original)
        return scope.knowledge.read(path)
    if scope.private_workspace is None:
        raise ValueError("Reading requires a bound main-store snapshot")
    target = (scope.wiki_dir / path).resolve()
    if not target.is_relative_to(scope.wiki_dir):
        raise ValueError("Invalid page path")
    return target.read_bytes()


def wiki_page_paths(scope: KnowledgeScope) -> tuple[str, ...]:
    """Enumerate safe Markdown names without hashing unrelated binary assets."""
    if scope.knowledge is not None:
        return tuple(sorted(path for path in scope.reading_view.files if path.endswith(".md")))
    if scope.private_workspace is None or not scope.private_workspace.permits_scope(scope.wiki_dir):
        raise ValueError("Knowledge enumeration requires a bound publication or private workspace")
    return tuple(
        sorted(
            path.relative_to(scope.wiki_dir).as_posix()
            for path in scope.wiki_dir.rglob("*.md")
            if path.is_file() and path.resolve().is_relative_to(scope.wiki_dir)
        )
    )
