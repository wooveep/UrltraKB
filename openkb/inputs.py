"""Private, stable input copies; their cleanup never owns a user's raw file."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from openkb.state import HashRegistry

TEXT_SOURCE_EXTENSIONS = {".md", ".markdown", ".txt", ".csv", ".xml", ".html", ".htm"}
IMAGE_SOURCE_EXTENSIONS = {".md", ".markdown", ".html", ".htm"}
OFFICE_SOURCE_EXTENSIONS = {".docx", ".doc", ".pptx"}
FROZEN_SOURCE_EXTENSIONS = {".pdf", *TEXT_SOURCE_EXTENSIONS, *OFFICE_SOURCE_EXTENSIONS}


def input_version(body_digest: str, assets: dict[str, str | None]) -> str:
    """Version the bytes consumed by an import, including absent local resources."""
    if not assets:
        return body_digest
    return hashlib.sha256(json.dumps([body_digest, assets], sort_keys=True).encode()).hexdigest()


def local_image_inputs(source: Path) -> dict[str, Path]:
    return _image_inputs(source, source.parent)


def _image_inputs(source: Path, base: Path) -> dict[str, Path]:
    from openkb.images import relative_image_paths

    if source.suffix.lower() not in IMAGE_SOURCE_EXTENSIONS:
        return {}
    try:
        references = None
        if source.suffix.lower() in {".html", ".htm"}:
            from openkb.html_images import html_image_references
            from openkb.text_encoding import decode_text

            text = decode_text(source.read_bytes())[0]
            references = html_image_references(text)
        else:
            text = source.read_bytes().decode("utf-8")
    except ValueError:
        # Admission/conversion owns the persisted malformed-input diagnosis.
        return {}
    return relative_image_paths(text, base, references=references)


def current_input_version(source: Path) -> str:
    return input_version(
        HashRegistry.hash_file(source),
        {
            ref: HashRegistry.hash_file(path) if path.is_file() else None
            for ref, path in local_image_inputs(source).items()
        },
    )


_preparation_root: ContextVar[Path | None] = ContextVar("openkb_preparation_root", default=None)


@contextmanager
def preparation_directory(path: Path | None) -> Iterator[None]:
    """Place private copies in a caller-owned execution directory when provided."""
    token = _preparation_root.set(path)
    try:
        yield
    finally:
        _preparation_root.reset(token)


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
    identity: Path

    def is_current(self) -> bool:
        if self.source.resolve() != self.identity:
            return False
        if HashRegistry.hash_file(self.source) != self.digest:
            return False
        if self.source.suffix.lower() not in IMAGE_SOURCE_EXTENSIONS:
            return True
        paths = _image_inputs(self.path, self.source.parent)
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
    # Freeze identity with the bytes. Later conversion/registration must not
    # follow a replacement symlink at this pathname to another document.
    identity = source.resolve()
    frozen = directory / "document" / source.name
    frozen.parent.mkdir(exist_ok=True)
    digest = copy_stable(source, frozen)
    resources = directory / "resources"
    if resources.exists():
        shutil.rmtree(resources)
    images = {}
    if source.suffix.lower() in IMAGE_SOURCE_EXTENSIONS:
        for index, (reference, original) in enumerate(_image_inputs(frozen, source.parent).items()):
            target = None
            image_digest = None
            if original.is_file():
                target = resources / str(index) / original.name
                target.parent.mkdir(parents=True)
                image_digest = copy_stable(original, target)
            images[reference] = PreparedImage(original, target, image_digest)
    ready = PreparedInput(source, frozen, digest, images, identity)
    if not ready.is_current():
        raise InputChanged(f"Input or related images changed while preparing: {source.name}")
    return ready


@contextmanager
def prepared_input(source: Path) -> Iterator[PreparedInput]:
    """Freeze primary bytes, image bytes and missing-image state before business."""
    with tempfile.TemporaryDirectory(
        prefix="openkb-input-", dir=_preparation_root.get()
    ) as directory:
        yield _prepare(source, Path(directory))


# Supported document extensions shared by import adapters
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".md",
    ".markdown",
    ".docx",
    ".doc",
    ".pptx",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".txt",
    ".csv",
    ".xml",
}
