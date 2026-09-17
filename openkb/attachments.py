"""Retained document references, separate from the parent's readable contents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class DocumentAttachment:
    source_id: str
    version_id: str
    part: str
    name: str

    def __post_init__(self):
        from openkb.sources import valid_id

        valid_id(self.source_id, source=True)
        valid_id(self.version_id)
        _validate_document_path(self.part, self.name)


def _validate_document_path(part, name):
    from openkb.inputs import SUPPORTED_EXTENSIONS

    if (
        not isinstance(part, str)
        or not part
        or PurePosixPath(part).is_absolute()
        or ".." in PurePosixPath(part).parts
        or not isinstance(name, str)
        or PurePosixPath(name).name != name
        or "\\" in name
        or PurePosixPath(name).suffix.lower() not in SUPPORTED_EXTENSIONS
    ):
        raise ValueError("Invalid document attachment path")


def validate_attachment_files(files, assets=None):
    from openkb.sources import valid_id

    if not isinstance(files, list) or not files or len(files) > 4096:
        raise ValueError("Invalid document attachment references")
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"part", "name", "blob", "parseable"}
            or type(item["parseable"]) is not bool
        ):
            raise ValueError("Invalid document attachment reference")
        _validate_document_path(item["part"], item["name"])
        valid_id(item["blob"])
        if assets is not None and item["blob"] not in assets:
            raise ValueError("Document attachment is not bound to this block")


def filename_text(name: str) -> str:
    """Display an untrusted filename without introducing Markdown or HTML markup."""
    import html
    import re

    return re.sub(r"([\\\[\]`*_])", r"\\\1", html.escape(name.replace("\n", " ")))
