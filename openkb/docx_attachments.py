"""Name retained embedded files without inspecting or parsing their contents."""

from __future__ import annotations

import io
from pathlib import PurePosixPath
from zipfile import ZipFile


def attachment_name(part: str, declared_name: str | None, content: bytes) -> str:
    """Use declared names; unnamed Office packages need only ZIP directory metadata."""
    if declared_name is not None:
        return declared_name
    with ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
    extension = next(
        (
            suffix
            for member, suffix in (
                ("word/document.xml", ".docx"),
                ("xl/workbook.xml", ".xlsx"),
                ("ppt/presentation.xml", ".pptx"),
            )
            if member in names
        ),
        ".zip",
    )
    return PurePosixPath(part).stem + extension


def attachment_depth(store, source):
    """Retain the nesting limit when each child is parsed by a separate worker."""
    seen, depth = set(), 0
    while source.origin.startswith("attachment:"):
        if source.source_id in seen:
            raise ValueError("docx_attachment_cycle")
        seen.add(source.source_id)
        source = store.current(source.origin.removeprefix("attachment:").split("/", 1)[0])
        depth += 1
    return depth
