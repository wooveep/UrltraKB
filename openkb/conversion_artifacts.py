"""Resolve a derived PDF and converter-specific provenance at its exact revision."""

from pathlib import Path

from openkb.ingest_records import UnitRevision
from openkb.normalization import normalization_id, read_normalization
from openkb.source_catalog import read_record, record_path


def read_conversion_artifacts(
    kb_dir: Path, unit_revision_id: str | None, source_revision_id: str
) -> dict:
    if unit_revision_id is None:
        return {}
    used = read_record(kb_dir, "unit-revisions", unit_revision_id, UnitRevision)
    if used.source_revision_id != source_revision_id:
        return {}
    identity = normalization_id(source_revision_id, used.processing_fingerprint)
    if not record_path(kb_dir, "normalizations", identity).exists():
        return {}
    directory, saved = read_normalization(kb_dir, identity)
    if saved.pdf_path is None:
        return {}
    result = {"internal_pdf_path": (directory / saved.pdf_path).relative_to(kb_dir).as_posix()}
    if saved.office_path:
        from openkb.office.records import OfficeConversion

        result["office"] = OfficeConversion.model_validate_json(
            (directory / saved.office_path).read_text("utf-8")
        ).model_dump(mode="json")
    elif saved.cnki_path:
        from openkb.cnki.records import CNKIConversion

        result["cnki"] = CNKIConversion.model_validate_json(
            (directory / saved.cnki_path).read_text("utf-8")
        ).model_dump(mode="json")
    return result
