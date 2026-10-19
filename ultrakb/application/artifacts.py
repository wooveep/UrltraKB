"""Browse and export committed artifacts; generate the existing HTML graph."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from ultrakb.application.execution import ExecutionContext
from ultrakb.knowledge_scope import KnowledgeScope, reading_scope, resolve_scope
from ultrakb.locks import kb_read_lock

if TYPE_CHECKING:
    from ultrakb.storage.artifact_store import ArtifactProjection, ArtifactRevision


def _artifact_path(kb_dir: Path, relative: str) -> Path:
    root = kb_dir.resolve()
    path = (root / relative).resolve()
    allowed = [root / name for name in ("output", "wiki/reports", "wiki/explorations")]
    if not any(path != base and path.is_relative_to(base) for base in allowed):
        raise ValueError("Select a generated artifact or a saved report")
    if not path.exists():
        raise FileNotFoundError(relative)
    return path


@dataclass(frozen=True)
class Artifact:
    path: str
    kind: str


@dataclass(frozen=True)
class ArtifactPage:
    items: tuple[Artifact, ...]
    next_after: str | None
    output_epoch: int
    publication_id: str

    def __iter__(self) -> Iterator[Artifact]:
        return iter(self.items)


def list_artifacts(
    kb_dir: Path,
    *,
    scope: KnowledgeScope | None = None,
    after: str | None = None,
    epoch: int | None = None,
    limit: int = 63,
) -> tuple[Artifact, ...] | ArtifactPage:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    from .storage_artifacts import artifact_catalog

    return artifact_catalog(kb_dir, scope, after=after, epoch=epoch, limit=limit)


def artifact_files(
    kb_dir: Path, relative: str, *, scope: KnowledgeScope | None = None, epoch: int | None = None
) -> tuple[str, ...]:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    from .storage_artifacts import StoredArtifact

    return tuple(StoredArtifact(kb_dir, relative, scope, epoch=epoch).paths())


def read_artifact(
    kb_dir: Path,
    relative: str,
    *,
    scope: KnowledgeScope | None = None,
    revision_id: str | None = None,
    epoch: int | None = None,
) -> str:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    from ultrakb.knowledge_scope import read_wiki_bytes
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.wiki_paths import wiki_path

    assert scope.knowledge is not None
    wiki_path(relative)
    if relative.startswith("output/"):
        return (
            ArtifactStore(scope.kb_dir, scope.knowledge.identity)
            .read(relative, revision_id=revision_id, epoch=epoch)
            .decode("utf-8")
        )
    if revision_id is not None or epoch is not None:
        raise ValueError("Select an output artifact revision or epoch")
    if relative.startswith(("wiki/explorations/", "wiki/reports/")):
        return read_wiki_bytes(scope, relative.removeprefix("wiki/")).decode("utf-8")
    raise ValueError("Select a generated artifact or a saved report")


def write_artifact_archive(
    kb_dir: Path,
    relative: str,
    output: BinaryIO,
    *,
    scope: KnowledgeScope | None = None,
    epoch: int | None = None,
) -> None:
    """Archive the same validated file set and directory layout for every entrypoint."""
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir = kb_dir.resolve()
    from .storage_artifacts import StoredArtifact

    StoredArtifact(kb_dir, relative, scope, epoch=epoch).archive(output)
    return


def export_artifact(
    kb_dir: Path,
    relative: str,
    destination: Path,
    *,
    scope: KnowledgeScope | None = None,
    epoch: int | None = None,
) -> Path:
    """Copy a file or a complete directory ZIP under a new, collision-free name."""
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    from ultrakb.export_paths import require_external_export

    kb_dir = kb_dir.resolve()
    destination = require_external_export(kb_dir, destination)
    if not destination.is_dir():
        raise NotADirectoryError(destination)
    from .storage_artifacts import StoredArtifact

    selected = StoredArtifact(kb_dir, relative, scope, epoch=epoch)
    with nullcontext() if selected else kb_read_lock(kb_dir / ".ultrakb"):
        source = Path(relative) if selected else _artifact_path(kb_dir, relative)
        is_directory = selected.is_directory if selected else source.is_dir()
        filename = source.name + ".zip" if is_directory else source.name
        number = 0
        while True:
            name = Path(filename)
            target = destination / (
                filename if number == 0 else f"{name.stem}_{number}{name.suffix}"
            )
            try:
                output = target.open("xb")
                break
            except FileExistsError:
                number += 1
        try:
            with output:
                if selected:
                    if is_directory:
                        selected.archive(output)
                    else:
                        selected.copy(relative, output)
                elif is_directory:
                    write_artifact_archive(kb_dir, relative, output)
                else:
                    with source.open("rb") as input_file:
                        shutil.copyfileobj(input_file, output)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise


def read_graph(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> dict:
    from ultrakb.visualize import build_graph

    selected = resolve_scope(kb_dir, scope)
    with reading_scope(selected):
        graph = build_graph(selected.wiki_dir, scope=selected)
        if selected.publication_id is not None:
            graph["publication_id"] = selected.publication_id
        return graph


def graph_html(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> str:
    from ultrakb.visualize import render_html

    return render_html(read_graph(kb_dir, scope=scope))


@dataclass(frozen=True)
class GraphResult:
    graph: dict
    path: Path | None = None
    artifact_revision: ArtifactRevision | None = None
    projection: ArtifactProjection | None = None
    execution_id: str | None = None
    reason_code: str | None = None
    confirmation_pending: bool = False


def generate_graph(
    kb_dir: Path, *, context: ExecutionContext | None = None, scope: KnowledgeScope | None = None
) -> GraphResult:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")

    kb_dir = kb_dir.resolve()
    from .storage_graph import generate_stored_graph

    return generate_stored_graph(kb_dir, resolve_scope(kb_dir, scope), context)
