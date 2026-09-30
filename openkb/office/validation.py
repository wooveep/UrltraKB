"""Validate the relationships among retained input, PDF and conversion provenance."""

import json
from pathlib import Path

import pymupdf

from openkb.office.records import OfficeConversion
from openkb.source_catalog import read_record
from openkb.source_records import SourceRevision


def validate_office_input(kb_dir: Path, directory: Path, saved) -> None:
    try:
        policy = json.loads(saved.fingerprint).get("office")
    except (ValueError, AttributeError):
        policy = None
    if not policy and saved.pdf_path is None and saved.office_path is None:
        return
    if not policy or not saved.pdf_path or not saved.office_path:
        raise ValueError("Retained Office input requires a PDF and conversion record")
    if len({saved.raw_path, saved.pdf_path, saved.office_path}) != 3:
        raise ValueError("Office original, PDF and provenance must be distinct artifacts")
    record = OfficeConversion.model_validate_json(
        (directory / saved.office_path).read_text("utf-8")
    )
    source = read_record(kb_dir, "source-revisions", saved.source_revision_id, SourceRevision)
    if (
        record.pdf_digest != saved.files[saved.pdf_path]
        or record.input_digest != saved.files[saved.raw_path]
        or record.input_digest != source.digest
        or record.processing_identity != policy
    ):
        raise ValueError("Retained Office artifacts do not match their processing identity")
    pdf = directory / saved.pdf_path
    with pdf.open("rb") as stream:
        if stream.read(8) != b"%PDF-1.7":
            raise ValueError("Retained Office PDF is not PDF 1.7")
    with pymupdf.open(pdf) as document:
        if document.needs_pass or document.page_count != record.pages:
            raise ValueError("Retained Office PDF pages do not match the conversion record")
