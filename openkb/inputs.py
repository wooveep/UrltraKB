"""Private, stable input copies; their cleanup never owns a user's raw file."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from openkb.state import HashRegistry


class InputChanged(ValueError):
    """The input changed during preparation and is not ready for this attempt."""


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


@contextmanager
def prepared_input(source: Path) -> Iterator[tuple[Path, str]]:
    """Keep a fixed source version until its consumer finishes.

    Logical identity and relative-resource lookup still use the original
    source path; this private pathname must never enter the document registry.
    """
    with tempfile.TemporaryDirectory(prefix="openkb-input-") as directory:
        frozen = Path(directory) / source.name
        digest = copy_stable(source, frozen)
        yield frozen, digest


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
