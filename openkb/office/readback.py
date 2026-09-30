"""Resolve Office artifacts at exactly the source revision used for this body."""

from pathlib import Path

from openkb.ingest_records import UnitRevision
from openkb.normalization import normalization_id, read_normalization
from openkb.office.records import OfficeConversion
from openkb.source_catalog import read_record, record_path


def read_office_artifacts(
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
    if saved.pdf_path is None or saved.office_path is None:
        return {}
    return {
        "internal_pdf_path": (directory / saved.pdf_path).relative_to(kb_dir).as_posix(),
        "office": OfficeConversion.model_validate_json(
            (directory / saved.office_path).read_text("utf-8")
        ).model_dump(mode="json"),
    }
