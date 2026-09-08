"""Browse and export committed artifacts; generate the existing HTML graph."""

from __future__ import annotations

import shutil
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from openkb.application.execution import ExecutionContext
from openkb.application.file_state import contained_paths
from openkb.config import _is_kb_dir
from openkb.locks import atomic_write_text, kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope


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


def list_artifacts(kb_dir: Path) -> tuple[Artifact, ...]:
    kb_dir = kb_dir.resolve()
    with kb_read_lock(kb_dir / ".openkb"):
        items = []
        grouped = []
        for folder, kind in (("skills", "Skill"), ("decks", "幻灯片")):
            for path in sorted((kb_dir / "output" / folder).glob("*")):
                if path.is_dir():
                    _artifact_path(kb_dir, str(path.relative_to(kb_dir)))
                    items.append(Artifact(path.relative_to(kb_dir).as_posix(), kind))
                    grouped.append(path)
        for folder in ("output", "wiki/reports", "wiki/explorations"):
            for path in sorted((kb_dir / folder).rglob("*")):
                if path.is_file() and not any(path.is_relative_to(group) for group in grouped):
                    _artifact_path(kb_dir, str(path.relative_to(kb_dir)))
                    items.append(Artifact(path.relative_to(kb_dir).as_posix(), "文件"))
        return tuple(items)


def artifact_files(kb_dir: Path, relative: str) -> tuple[str, ...]:
    with kb_read_lock(kb_dir / ".openkb"):
        path = _artifact_path(kb_dir, relative)
        files = [p for p in sorted(path.rglob("*")) if p.is_file()] if path.is_dir() else [path]
        for file in files:
            _artifact_path(kb_dir, str(file.relative_to(kb_dir)))
        return tuple(file.relative_to(kb_dir).as_posix() for file in files)


def read_artifact(kb_dir: Path, relative: str) -> str:
    with kb_read_lock(kb_dir / ".openkb"):
        path = _artifact_path(kb_dir, relative)
        if not path.is_file():
            raise ValueError("Select a text file in the artifact")
        return path.read_text(encoding="utf-8")


def export_artifact(kb_dir: Path, relative: str, destination: Path) -> Path:
    """Copy a file or a complete directory ZIP under a new, collision-free name."""
    kb_dir, destination = kb_dir.resolve(), destination.resolve()
    if destination.is_relative_to(kb_dir) or any(
        _is_kb_dir(parent)
        or any(
            (parent / ".openkb" / marker).exists()
            for marker in (
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
    with kb_read_lock(kb_dir / ".openkb"):
        source = _artifact_path(kb_dir, relative)
        files = artifact_files(kb_dir, relative)
        filename = source.name + ".zip" if source.is_dir() else source.name
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
                if source.is_dir():
                    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                        for file in files:
                            path = kb_dir / file
                            archive.write(path, path.relative_to(source.parent).as_posix())
                else:
                    with source.open("rb") as input_file:
                        shutil.copyfileobj(input_file, output)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise


def read_graph(kb_dir: Path) -> dict:
    from openkb.visualize import build_graph

    with kb_read_lock(kb_dir / ".openkb"):
        return build_graph(kb_dir / "wiki")


def graph_html(kb_dir: Path) -> str:
    from openkb.visualize import render_html

    return render_html(read_graph(kb_dir))


@dataclass(frozen=True)
class GraphResult:
    graph: dict
    path: Path | None = None


def generate_graph(kb_dir: Path, *, context: ExecutionContext | None = None) -> GraphResult:
    from openkb.visualize import build_graph, render_html

    kb_dir = kb_dir.resolve()
    with kb_ingest_lock(
        kb_dir / ".openkb",
        cancelled=context.cancelled if context else None,
        on_wait=context.waiting if context else None,
    ):
        path = kb_dir / "output/visualize/graph.html"
        contained_paths(kb_dir, [path])
        with context.begin(kb_dir) if context else nullcontext():
            if context:
                context.on_event({"stage": "generating_graph"})
            graph = build_graph(kb_dir / "wiki")
            if not graph["nodes"]:
                return GraphResult(graph)
            html = render_html(graph)
            with mutation_scope(kb_dir, [path], operation="generate-graph"):
                atomic_write_text(path, html)
            return GraphResult(graph, path)
