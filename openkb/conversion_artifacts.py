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
    return read_conversion_files(
        kb_dir,
        directory / saved.pdf_path,
        office_path=directory / saved.office_path if saved.office_path else None,
        cnki_path=directory / saved.cnki_path if saved.cnki_path else None,
    )


def read_conversion_files(
    kb_dir: Path, pdf_path: Path, *, office_path=None, cnki_path=None
) -> dict:
    """Read verified conversion provenance for published or retained-only originals."""
    from openkb.file_state import contained_paths
    from openkb.state import HashRegistry

    contained_paths(kb_dir, [path for path in (pdf_path, office_path, cnki_path) if path])
    result = {"internal_pdf_path": pdf_path.relative_to(kb_dir).as_posix()}
    if office_path:
        from openkb.office.records import OfficeConversion

        record = OfficeConversion.model_validate_json(office_path.read_text("utf-8"))
        result["office"] = record.model_dump(mode="json")
    elif cnki_path:
        from openkb.cnki.records import CNKIConversion

        record = CNKIConversion.model_validate_json(cnki_path.read_text("utf-8"))
        result["cnki"] = record.model_dump(mode="json")
    else:
        return result
    if HashRegistry.hash_file(pdf_path) != record.pdf_digest:
        raise ValueError("Retained PDF does not match its conversion provenance")
    return result
