"""A retained complete inventory is separate from individual worksheet outcomes."""

from openkb.ingest_records import UnitRevision
from openkb.mutation import mutation_scope
from openkb.source_catalog import read_record, record_path, write_record
from openkb.workbooks.records import WorkbookSnapshot


def retain_workbook(kb_dir, admission, prepared):
    identity = admission.revision.source_revision_id
    path = record_path(kb_dir, "workbooks", identity)
    if path.exists():
        saved = read_record(kb_dir, "workbooks", identity, WorkbookSnapshot)
        if saved.digest != admission.revision.digest:
            raise ValueError("Workbook inventory belongs to another original")
        return saved
    from openkb.workbooks.xlsx import read_xlsx

    try:
        saved = WorkbookSnapshot(
            source_revision_id=identity, digest=prepared.digest, sheets=read_xlsx(prepared.path)
        )
    except Exception as exc:
        saved = WorkbookSnapshot(
            source_revision_id=identity,
            digest=prepared.digest,
            sheets=(),
            error=f"Workbook inventory: {type(exc).__name__}: {exc}",
        )
    from openkb.catalog_schema import CatalogSchema, catalog_schema_path

    schema = catalog_schema_path(kb_dir)
    with mutation_scope(kb_dir, [path, schema], operation="retain-workbook-inventory"):
        write_record(schema, CatalogSchema())
        write_record(path, saved)
    return saved


def inventory_failure(kb_dir, source_revision_id):
    path = record_path(kb_dir, "workbooks", source_revision_id)
    if not path.exists():
        return None
    return read_record(kb_dir, "workbooks", source_revision_id, WorkbookSnapshot).error


def unit_sheet(kb_dir, unit, source_revision_id):
    if unit.key == "body":
        return None
    path = record_path(kb_dir, "workbooks", source_revision_id)
    if not path.exists():
        return None
    workbook = read_record(kb_dir, "workbooks", source_revision_id, WorkbookSnapshot)
    return next((sheet for sheet in workbook.sheets if sheet.key == unit.key), None)


def unit_name(kb_dir, unit, unit_revision_id, fallback):
    revision = read_record(kb_dir, "unit-revisions", unit_revision_id, UnitRevision)
    sheet = unit_sheet(kb_dir, unit, revision.source_revision_id)
    return sheet.name if sheet else fallback
