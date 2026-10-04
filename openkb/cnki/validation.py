"""Validate retained CNKI relationships without running a converter again."""

import json
from pathlib import Path

from openkb.cnki.engine import detect_format, inspect_pdf
from openkb.cnki.records import CNKIConversion
from openkb.source_catalog import read_source_revision


def validate_cnki_input(kb_dir: Path, directory: Path, saved) -> None:
    policy = json.loads(saved.fingerprint).get("cnki")
    if not policy or not saved.pdf_path or not saved.cnki_path or saved.office_path:
        raise ValueError("Retained CNKI input requires its own PDF and conversion record")
    if len({saved.raw_path, saved.pdf_path, saved.cnki_path}) != 3:
        raise ValueError("CNKI original, PDF and receipt must be distinct artifacts")
    record = CNKIConversion.model_validate_json((directory / saved.cnki_path).read_text("utf-8"))
    source = read_source_revision(kb_dir, saved.source_revision_id)
    with (directory / saved.raw_path).open("rb") as stream:
        kind = detect_format(stream.read(8))
    if (
        source.source_format not in {"caj", "kdh"}
        or record.internal_format != kind
        or record.input_digest != source.digest
        or record.input_digest != saved.files[saved.raw_path]
        or record.pdf_digest != saved.files[saved.pdf_path]
        or record.processing_identity != policy
    ):
        raise ValueError("Retained CNKI artifacts do not match their input or processing identity")
    inspect_pdf(directory / saved.pdf_path, declared_pages=record.pages)
