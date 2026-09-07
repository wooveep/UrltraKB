"""Private, stable input copies; their cleanup never owns a user's raw file."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from openkb.state import HashRegistry


class InputChanged(ValueError):
    """The input changed during preparation and is not ready for this attempt."""


def validate_source_root(source: Path, root: Path | None) -> None:
    if root is not None and (
        root.resolve() != root or source.is_symlink() or not source.resolve().is_relative_to(root)
    ):
        raise ValueError("Watched input moved outside its original raw directory")


def copy_stable(source: Path, target: Path) -> str:
    """Copy bytes and verify their version before using them for any processing."""
    before = source.stat()
    shutil.copy2(source, target)
    digest = HashRegistry.hash_file(target)
    current = HashRegistry.hash_file(source)
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or digest != current:
        target.unlink(missing_ok=True)
        raise InputChanged(f"Input changed while preparing: {source.name}")
    return digest


@dataclass(frozen=True)
class PreparedImage:
    original: Path
    path: Path | None
    digest: str | None


@dataclass(frozen=True)
class PreparedInput:
    """Owned bytes and related resources; original paths retain logical identity."""

    source: Path
    path: Path
    digest: str
    images: dict[str, PreparedImage]

    def is_current(self) -> bool:
        from openkb.images import relative_image_paths

        if HashRegistry.hash_file(self.source) != self.digest:
            return False
        if self.source.suffix.lower() not in {".md", ".markdown"}:
            return True
        paths = relative_image_paths(self.path.read_text(encoding="utf-8"), self.source.parent)
        if paths.keys() != self.images.keys():
            return False
        for reference, path in paths.items():
            image = self.images[reference]
            digest = HashRegistry.hash_file(path) if path.is_file() else None
            if path != image.original or digest != image.digest:
                return False
        return True

    def refresh(self) -> PreparedInput:
        return _prepare(self.source, self.path.parent.parent)


def _prepare(source: Path, directory: Path) -> PreparedInput:
    from openkb.images import relative_image_paths

    frozen = directory / "document" / source.name
    frozen.parent.mkdir(exist_ok=True)
    digest = copy_stable(source, frozen)
    resources = directory / "resources"
    if resources.exists():
        shutil.rmtree(resources)
    images = {}
    if source.suffix.lower() in {".md", ".markdown"}:
        for index, (reference, original) in enumerate(
            relative_image_paths(frozen.read_text(encoding="utf-8"), source.parent).items()
        ):
            target = None
            image_digest = None
            if original.is_file():
                target = resources / str(index) / original.name
                target.parent.mkdir(parents=True)
                image_digest = copy_stable(original, target)
            images[reference] = PreparedImage(original, target, image_digest)
    ready = PreparedInput(source, frozen, digest, images)
    if not ready.is_current():
        raise InputChanged(f"Input or related images changed while preparing: {source.name}")
    return ready


@contextmanager
def prepared_input(source: Path) -> Iterator[PreparedInput]:
    """Freeze primary bytes, image bytes and missing-image state before business."""
    with tempfile.TemporaryDirectory(prefix="openkb-input-") as directory:
        yield _prepare(source, Path(directory))


# Supported document extensions shared by import adapters
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".md",
    ".markdown",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".txt",
    ".csv",
}
