"""Reconcile retained worksheet identities using a complete workbook inventory."""

import uuid

from openkb.source_catalog import read_record
from openkb.source_records import SourceRevision
from openkb.workbooks.records import WorkbookSnapshot


def previous_workbook(kb_dir, admission):
    candidates = []
    for path in (kb_dir / ".openkb/catalog/workbooks").glob("*.json"):
        if path.stem == admission.revision.source_revision_id:
            continue
        revision = read_record(kb_dir, "source-revisions", path.stem, SourceRevision)
        if (
            revision.source_id != admission.source.source_id
            or revision.created_at >= admission.revision.created_at
        ):
            continue
        saved = read_record(kb_dir, "workbooks", path.stem, WorkbookSnapshot)
        if not saved.error:
            candidates.append((revision.created_at, saved))
    return max(candidates, key=lambda value: value[0])[1] if candidates else None


def reconcile_sheets(sheets, previous):
    """Names anchor reordering; native IDs are hints, never ordinal-only identity.

    A sole unmatched rename can be inferred in an otherwise matched inventory
    when its complete cell/layout evidence is unchanged. Ambiguous replacements
    receive fresh identities; equal-content sheets are never collapsed.
    """
    old = list(previous.sheets) if previous else []
    matched = {}
    available = {sheet.key: sheet for sheet in old}
    by_name = {sheet.name.casefold(): sheet for sheet in old}
    native_ids_reassigned = False
    for index, sheet in enumerate(sheets):
        known = by_name.get(sheet.name.casefold())
        if known:
            matched[index] = (known, "stable_name")
            available.pop(known.key)
            native_ids_reassigned |= known.native_id != sheet.native_id
    if matched and not native_ids_reassigned and len(available) == 1 and len(sheets) == len(old):
        for index, sheet in enumerate(sheets):
            if index in matched or sheet.native_id is None:
                continue
            candidates = [item for item in available.values() if item.native_id == sheet.native_id]
            if len(candidates) == 1:
                known = candidates[0]
                matched[index] = (known, "native_id_with_inventory_continuity")
                available.pop(known.key)
    remaining = [index for index in range(len(sheets)) if index not in matched]
    if len(remaining) == len(available) == 1 and len(sheets) == len(old):
        index = remaining[0]
        known = next(iter(available.values()))
        candidate = sheets[index]
        fields = ("cells", "merged_ranges", "hidden_rows", "hidden_columns", "has_objects", "state")
        if (
            not known.error
            and not candidate.error
            and all(getattr(candidate, field) == getattr(known, field) for field in fields)
        ):
            matched[index] = (known, "unique_rename_in_complete_inventory")
    result = []
    for index, sheet in enumerate(sheets):
        known, basis = matched.get(index, (None, "new_identity"))
        result.append(
            sheet.model_copy(
                update={
                    "key": known.key if known else "sheet:" + uuid.uuid4().hex,
                    "identity_basis": basis,
                    "identity_from_revision_id": previous.source_revision_id if known else None,
                }
            )
        )
    return tuple(result)
