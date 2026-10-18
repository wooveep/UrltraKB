"""Browse and export committed artifacts; generate the existing HTML graph."""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.file_state import contained_paths
from ultrakb.config import _is_kb_dir
from ultrakb.knowledge_scope import KnowledgeScope, reading_scope, resolve_scope
from ultrakb.locks import atomic_write_text, kb_ingest_lock, kb_read_lock
from ultrakb.mutation import mutation_scope

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
    if scope is not None and scope.knowledge is not None:
        from .storage_artifacts import artifact_catalog

        return artifact_catalog(kb_dir, scope, after=after, epoch=epoch, limit=limit)
    if after is not None or epoch is not None:
        raise ValueError("Artifact catalog cursors require a main-store scope")
    kb_dir = kb_dir.resolve()
    with kb_read_lock(kb_dir / ".ultrakb"):
        items = []
        grouped = []
        for folder, kind in (("skills", "Skill"), ("decks", "幻灯片")):
            for path in sorted((kb_dir / "output" / folder).glob("*")):
                if path.is_dir() and not path.name.endswith("-workspace"):
                    _artifact_path(kb_dir, str(path.relative_to(kb_dir)))
                    items.append(Artifact(path.relative_to(kb_dir).as_posix(), kind))
                    grouped.append(path)
        for folder in ("output", "wiki/reports", "wiki/explorations"):
            for path in sorted((kb_dir / folder).rglob("*")):
                if (
                    path.is_file()
                    and not any(path.is_relative_to(group) for group in grouped)
                    and not any(
                        part.startswith(".") or part.endswith("-workspace")
                        for part in path.relative_to(kb_dir).parts
                    )
                ):
                    _artifact_path(kb_dir, str(path.relative_to(kb_dir)))
                    relative = path.relative_to(kb_dir).as_posix()
                    kind = (
                        "检查报告"
                        if folder == "wiki/reports"
                        else "探索笔记"
                        if folder == "wiki/explorations"
                        else "知识图谱"
                        if relative == "output/visualize/graph.html"
                        else "文件"
                    )
                    items.append(Artifact(relative, kind))
        return tuple(items)


def artifact_files(
    kb_dir: Path, relative: str, *, scope: KnowledgeScope | None = None, epoch: int | None = None
) -> tuple[str, ...]:
    if scope is not None and scope.knowledge is not None:
        from .storage_artifacts import StoredArtifact

        return tuple(StoredArtifact(kb_dir, relative, scope, epoch=epoch).paths())
    if epoch is not None:
        raise ValueError("Artifact epochs require a main-store scope")
    with kb_read_lock(kb_dir / ".ultrakb"):
        path = _artifact_path(kb_dir, relative)
        files = [p for p in sorted(path.rglob("*")) if p.is_file()] if path.is_dir() else [path]
        for file in files:
            _artifact_path(kb_dir, str(file.relative_to(kb_dir)))
        return tuple(file.relative_to(kb_dir).as_posix() for file in files)


def read_artifact(
    kb_dir: Path,
    relative: str,
    *,
    scope: KnowledgeScope | None = None,
    revision_id: str | None = None,
    epoch: int | None = None,
) -> str:
    if scope is not None and scope.knowledge is not None:
        from ultrakb.knowledge_scope import read_wiki_bytes
        from ultrakb.storage.artifact_store import ArtifactStore
        from ultrakb.storage.wiki_paths import wiki_path

        scope = resolve_scope(kb_dir, scope)
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
    if revision_id is not None or epoch is not None:
        raise ValueError("Artifact version reads require a main-store scope")
    with kb_read_lock(kb_dir / ".ultrakb"):
        path = _artifact_path(kb_dir, relative)
        if not path.is_file():
            raise ValueError("Select a text file in the artifact")
        return path.read_text(encoding="utf-8")


def write_artifact_archive(
    kb_dir: Path,
    relative: str,
    output: BinaryIO,
    *,
    scope: KnowledgeScope | None = None,
    epoch: int | None = None,
) -> None:
    """Archive the same validated file set and directory layout for every entrypoint."""
    kb_dir = kb_dir.resolve()
    if scope is not None and scope.knowledge is not None:
        from .storage_artifacts import StoredArtifact

        StoredArtifact(kb_dir, relative, scope, epoch=epoch).archive(output)
        return
    if epoch is not None:
        raise ValueError("Artifact epochs require a main-store scope")
    with kb_read_lock(kb_dir / ".ultrakb"):
        source = _artifact_path(kb_dir, relative)
        files = artifact_files(kb_dir, relative)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                path = kb_dir / file
                archive.write(path, path.relative_to(source.parent).as_posix())


def export_artifact(
    kb_dir: Path,
    relative: str,
    destination: Path,
    *,
    scope: KnowledgeScope | None = None,
    epoch: int | None = None,
) -> Path:
    """Copy a file or a complete directory ZIP under a new, collision-free name."""
    kb_dir, destination = kb_dir.resolve(), destination.resolve()
    if destination.is_relative_to(kb_dir) or any(
        _is_kb_dir(parent)
        or any(
            (parent / ".ultrakb" / marker).exists()
            for marker in (
                "kb.sqlite",
                "config.yaml",
                "hashes.json",
                "needs-repair.json",
                "initializing.json",
                "journal",
            )
        )
        for parent in (destination, *destination.parents)
    ):
        raise ValueError("Choose an export directory outside every knowledge base")
    if not destination.is_dir():
        raise NotADirectoryError(destination)
    if scope is not None and scope.knowledge is not None:
        from .storage_artifacts import StoredArtifact

        selected = StoredArtifact(kb_dir, relative, scope, epoch=epoch)
    else:
        if epoch is not None:
            raise ValueError("Artifact epochs require a main-store scope")
        selected = None
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
    from ultrakb.visualize import build_graph, render_html

    kb_dir = kb_dir.resolve()
    if scope is not None and scope.knowledge is not None:
        from .storage_graph import generate_stored_graph

        return generate_stored_graph(kb_dir, resolve_scope(kb_dir, scope), context)
    with kb_ingest_lock(
        kb_dir / ".ultrakb",
        cancelled=context.cancelled if context else None,
        on_wait=context.waiting if context else None,
    ):
        path = kb_dir / "output/visualize/graph.html"
        contained_paths(kb_dir, [path])
        with context.begin(kb_dir) if context else nullcontext():
            if context:
                context.on_event({"stage": "generating_graph"})
            graph = build_graph(resolve_scope(kb_dir, scope).wiki_dir)
            if not graph["nodes"]:
                return GraphResult(graph)
            html = render_html(graph)
            with mutation_scope(kb_dir, [path], operation="generate-graph"):
                atomic_write_text(path, html)
            return GraphResult(graph, path)
