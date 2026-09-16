"""Browse and export committed artifacts; generate the existing HTML graph."""

from __future__ import annotations

import io
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
                    if path.name.endswith("-workspace"):
                        for iteration in sorted(path.glob("iteration-*")):
                            if iteration.is_dir():
                                _artifact_path(kb_dir, str(iteration.relative_to(kb_dir)))
                                items.append(
                                    Artifact(
                                        iteration.relative_to(kb_dir).as_posix(), kind + "归档"
                                    )
                                )
                    else:
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


def artifact_quality(kb_dir: Path, relative: str) -> dict:
    """Read current checks; missing history is unknown and edited files are stale."""
    from openkb.artifact_quality import read_quality

    kb_dir = kb_dir.resolve()
    with kb_read_lock(kb_dir / ".openkb"):
        return read_quality(kb_dir, _artifact_path(kb_dir, relative))


def delete_artifact(kb_dir: Path, relative: str) -> None:
    """Explicitly delete the selected artifact and its own evidence retention roots."""
    from openkb.application.file_state import file_versions
    from openkb.artifact_quality import quality_path
    from openkb.skill.marketplace import regenerate_marketplace

    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
        target = _artifact_path(root, relative)
        if target in {root / "output/skills", root / "output/decks"}:
            raise ValueError("Select an individual artifact or its archive workspace")
        # Exact path identities allow explicit deletion even if this artifact's
        # record is damaged. Unrelated records do not grant deletion authority.
        members = [target, *target.rglob("*")] if target.is_dir() else [target]
        records = [path for member in members if (path := quality_path(root, member)).is_file()]
        paths = [target, *records, root / ".claude-plugin/marketplace.json"]
        file_versions(root, paths)
        with mutation_scope(root, paths, operation="delete-artifact"):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            for path in records:
                path.unlink()
            regenerate_marketplace(root)


def artifact_archive(
    kb_dir: Path, relative: str, *, include_evidence: bool = False, strip_root: bool = False
) -> bytes:
    """Build a locked archive for download, including the legacy Skill layout."""
    from openkb.artifact_export import portable_files

    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        target = _artifact_path(root, relative)
        files = artifact_files(root, relative)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if include_evidence:
                for name, content in portable_files(root, target, files).items():
                    archive.writestr(name, content)
            else:
                base = target if strip_root and target.is_dir() else target.parent
                for file in files:
                    archive.write(root / file, (root / file).relative_to(base).as_posix())
        return output.getvalue()


def export_artifact(
    kb_dir: Path, relative: str, destination: Path, *, include_evidence: bool = False
) -> Path:
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
        artifact_files(kb_dir, relative)
        filename = (
            source.name + "-with-evidence.zip"
            if include_evidence
            else source.name + ".zip"
            if source.is_dir()
            else source.name
        )
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
                if include_evidence:
                    output.write(artifact_archive(kb_dir, relative, include_evidence=True))
                elif source.is_dir():
                    output.write(artifact_archive(kb_dir, relative))
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
