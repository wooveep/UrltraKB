"""Aggregate worksheet checkpoints against the frozen, complete source inventory."""

from dataclasses import dataclass
from pathlib import Path

from openkb.ingest_records import UnitPublication, UnitRevision
from openkb.source_catalog import read_record, read_source_revision, record_path
from openkb.source_records import Source, SourceRevision
from openkb.unit_publication import list_source_units, read_unit_publication
from openkb.workbooks.records import WorkbookSnapshot


def read_workbook(kb_dir: Path, revision: SourceRevision) -> WorkbookSnapshot:
    workbook = read_record(kb_dir, "workbooks", revision.source_revision_id, WorkbookSnapshot)
    if workbook.digest != revision.digest:
        raise ValueError("Workbook inventory belongs to another original")
    return workbook


@dataclass(frozen=True)
class WorkbookProgress:
    workbook: WorkbookSnapshot | None
    publications: tuple[UnitPublication, ...] = ()
    complete: bool = False

    @property
    def finished(self) -> bool:
        return self.complete and all(
            state.status in {"completed", "empty", "retired"} for state in self.publications
        )

    @property
    def empty(self) -> bool:
        return (
            self.finished
            and self.workbook is not None
            and all(sheet.content_state == "empty" for sheet in self.workbook.sheets)
            and all(state.status in {"empty", "retired"} for state in self.publications)
        )


def workbook_progress(kb_dir: Path, source: Source, view_id: str) -> WorkbookProgress | None:
    """Coverage includes not-yet-created sheets and required old-sheet retirements."""
    revision = read_source_revision(kb_dir, source.target_revision_id)
    if revision.source_format not in {"xls", "xlsx"}:
        return None
    if revision.source_id != source.source_id:
        raise ValueError("Workbook revision belongs to another source")
    if not record_path(kb_dir, "workbooks", revision.source_revision_id).exists():
        return WorkbookProgress(None)
    workbook = read_workbook(kb_dir, revision)
    if workbook.error:
        return WorkbookProgress(workbook)
    units = list_source_units(kb_dir, source.source_id)
    complete = bool(units) and {sheet.key for sheet in workbook.sheets} <= {
        unit.key for unit in units
    }
    states = []
    for unit in units:
        target = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
        if target.unit_id != unit.unit_id:
            raise ValueError("Worksheet revision belongs to another unit")
        if (
            target.source_revision_id != revision.source_revision_id
            or target.annotation_id != source.annotation_id
        ):
            complete = False
            continue
        try:
            state = read_unit_publication(kb_dir, unit.unit_id, view_id)
        except FileNotFoundError:
            complete = False
            continue
        if state.target_revision_id != unit.target_revision_id:
            complete = False
            continue
        states.append(state)
    return WorkbookProgress(workbook, tuple(states), complete)


def retain_workbook_normalization(kb_dir: Path, admission, view_id: str) -> str:
    """Use a current retained sheet as the source's frozen processing basis."""
    from openkb.normalization import normalization_id, retain_published_normalization

    workbook = read_workbook(kb_dir, admission.revision)
    if workbook.error:
        raise ValueError("Workbook has no reliable retained worksheet inventory")
    keys = {sheet.key for sheet in workbook.sheets}
    for unit in list_source_units(kb_dir, admission.source.source_id):
        target = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
        if target.unit_id != unit.unit_id:
            raise ValueError("Worksheet revision belongs to another unit")
        if (
            unit.key not in keys
            or target.source_revision_id != admission.revision.source_revision_id
        ):
            continue
        identity = normalization_id(target.source_revision_id, target.processing_fingerprint)
        if not record_path(kb_dir, "normalizations", identity).exists():
            try:
                state = read_unit_publication(kb_dir, unit.unit_id, view_id)
            except FileNotFoundError:
                continue
            if state.successful_revision_id != unit.target_revision_id:
                continue
        return retain_published_normalization(kb_dir, admission, unit, view_id)
    raise ValueError("Workbook has no retained normalized worksheet for the current input")
