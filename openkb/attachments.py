"""Retained document references, separate from the parent's readable contents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


def parent_blocks(parsed):
    """Retain historical parses, excluding expanded child bodies from new parent work."""
    return tuple(block for block in parsed.blocks if "attachment" not in block.location)


def attachment_diagnostic(row):
    return "attachment" in row.get("location", {}) or row["reason"].startswith(
        ("docx_attachment:", "docx_attachment_unparsed:")
    )


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
    if (
        not isinstance(part, str)
        or not part
        or PurePosixPath(part).is_absolute()
        or ".." in PurePosixPath(part).parts
        or "\\" in part
        or "\x00" in part
        or not isinstance(name, str)
        or name in {"", ".", ".."}
        or PurePosixPath(name).name != name
        or "\\" in name
        or "\x00" in name
    ):
        raise ValueError("Invalid document attachment path")


def validate_attachment_files(files, assets=None):
    from openkb.sources import valid_id

    if not isinstance(files, list) or not files or len(files) > 4096:
        raise ValueError("Invalid document attachment references")
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item)
            not in (
                {"part", "name", "blob", "parseable"},
                {"part", "name", "blob", "parseable", "extraction", "reason"},
            )
            or type(item["parseable"]) is not bool
        ):
            raise ValueError("Invalid document attachment reference")
        if "extraction" in item and (
            item["extraction"] != "raw_object"
            or not isinstance(item["reason"], str)
            or not item["reason"].startswith("docx_")
            or item["parseable"]
        ):
            raise ValueError("Invalid raw attachment object reference")
        _validate_document_path(item["part"], item["name"])
        valid_id(item["blob"])
        if assets is not None and item["blob"] not in assets:
            raise ValueError("Document attachment is not bound to this block")


def filename_text(name: str) -> str:
    """Display an untrusted filename without introducing Markdown or HTML markup."""
    import html
    import re

    return re.sub(r"([\\\[\]`*_])", r"\\\1", html.escape(name.replace("\n", " ")))
