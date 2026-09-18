"""Preflight actual storage volumes and retain actionable resource diagnostics."""

import json
import shutil
from contextlib import contextmanager
from pathlib import Path

from openkb.log import logger
from openkb.processing import ProcessingIncomplete

DISK_RESERVE = 1024**3
CONTRACT_BYTES = 256 * 1024**2


def check_parser(path):
    from zipfile import BadZipFile, ZipFile

    from openkb.resource_budget import check_memory

    estimated = path.stat().st_size * 4
    if path.suffix.lower() in {".docx", ".pptx", ".xlsx"}:
        try:
            with ZipFile(path) as archive:
                # XML object graphs and decompression buffers coexist briefly.
                estimated = sum(
                    member.file_size * (8 if member.filename.endswith(".xml") else 2)
                    for member in archive.infolist()
                )
        except BadZipFile:
            pass  # The existing parser owns malformed-container diagnostics.
    check_memory(estimated, stage="parsing")
    check_disk(path.parent, estimated, stage="parsing", operation="parser_outputs")


def json_size(value):
    return sum(len(part.encode("utf-8")) for part in json.JSONEncoder().iterencode(value))


def check_disk(path, expected_bytes, *, stage, operation):
    """Keep space for both the atomic temporary file and its committed copy."""
    location = Path(path).resolve()
    while not location.exists():
        location = location.parent
    available = shutil.disk_usage(location).free
    required = max(DISK_RESERVE, 2 * expected_bytes)
    if available < required:
        logger.warning(
            "Resource limit stage=%s operation=%s volume=%s free_bytes=%s required_bytes=%s",
            stage,
            operation,
            location,
            available,
            required,
        )
        raise ProcessingIncomplete("resource_disk_insufficient", stage)


@contextmanager
def resource_operation(stage, operation):
    try:
        yield
    except (OSError, MemoryError) as exc:
        logger.error(
            "Resource failure stage=%s operation=%s error=%s errno=%s",
            stage,
            operation,
            type(exc).__name__,
            getattr(exc, "errno", None),
            exc_info=True,
        )
        raise
