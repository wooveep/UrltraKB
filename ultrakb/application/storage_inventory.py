"""Managed source targets alongside their exact fixed-publication knowledge basis."""

from pathlib import PurePosixPath

from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.pending_store import PendingStore


def catalog_sources(catalog, *, include_removed=False):
    after = ""
    while sources := catalog.source_page(after=after, include_removed=include_removed):
        yield from sources
        after = sources[-1].source_id


def unit_inventory(catalog, target, actual, scope):
    processing = catalog.processing(target.processing_id)
    used = catalog.processing(actual.processing_id) if actual else None
    decision = used.decision if used else None
    content_state = processing.content_state
    exclusion = catalog.unit_exclusion(target.unit_revision_id)
    complete = actual is not None and actual.unit_revision_id == target.unit_revision_id
    status = (
        "retired"
        if exclusion and exclusion[0] == "withdrawn"
        else "empty"
        if exclusion
        else "completed"
        if complete
        else "empty"
        if content_state == "empty"
        else "failed"
        if content_state in {"parse_failed", "unreadable"}
        else "blocked"
        if content_state == "objects_only"
        else "pending"
    )
    work = PendingStore(scope.kb_dir, scope.knowledge.identity).unit_status(target.unit_revision_id)
    if work and work[0] not in {"completed", "stale"}:
        status = {"dispatched": "pending", "started": "processing"}.get(work[0], work[0])
    measured = decision or processing.decision
    from ultrakb.storage.compile_outcomes import read_unit_outcome

    outcome = read_unit_outcome(
        scope.kb_dir,
        scope.knowledge.identity,
        target.unit_revision_id,
        scope.publication_id,
        scope.view_id,
    )
    return {
        "unit_id": target.unit_id,
        "key": target.key,
        "name": target.label or target.doc_name,
        "doc_name": target.doc_name,
        "target_revision_id": target.unit_revision_id,
        "target_source_revision_id": target.source_revision_id,
        "successful_revision_id": actual.unit_revision_id if actual else None,
        "successful_source_revision_id": actual.source_revision_id if actual else None,
        "knowledge_revision_id": scope.knowledge.publication.revision_id if actual else None,
        "view_id": scope.view_id,
        "status": status,
        "content_state": content_state,
        "tokens": measured.measurement_value if measured.measurement_unit == "token" else None,
        "pages": measured.measurement_value
        if measured.measurement_unit == "physical_page"
        else None,
        "characters": measured.characters,
        "error_type": work[1] if work else None,
        "quality": outcome.quality,
        "unfinished": outcome.unfinished,
        "quality_known": outcome.quality_known,
        "processing": decision.model_dump(mode="json") if decision else None,
        "target_processing": processing.decision.model_dump(mode="json"),
        "length_class": decision.length_class if decision else processing.decision.length_class,
        "execution_mode": decision.execution_mode
        if decision
        else processing.decision.execution_mode,
    }


def source_inventory(scope):
    from types import SimpleNamespace

    from .sources import source_status

    bound = scope.knowledge
    catalog = CatalogStore(scope.kb_dir, bound.identity)
    active = set(bound.unit_revisions())
    actuals = {}
    for unit in sorted(bound.input_units(), key=lambda item: item.unit_revision_id not in active):
        actuals.setdefault(unit.unit_id, unit)
    documents = []
    for source in catalog_sources(catalog, include_removed=True):
        targets = catalog.source_units(source.source_id)
        units = [
            unit_inventory(catalog, unit, actuals.get(unit.unit_id), scope) for unit in targets
        ]
        if source.removed or source.contribution_empty:
            for unit in units:
                unit["status"] = "retired" if source.removed else "empty"
        actual = next((actuals[unit.unit_id] for unit in targets if unit.unit_id in actuals), None)
        inventory_error = None
        if PurePosixPath(source.label).suffix.lower() in {".xlsx", ".xls"}:
            from ultrakb.storage.workbook_store import WorkbookStore

            workbook = WorkbookStore(scope.kb_dir, bound.identity).read(source.revision_id)
            inventory_error = workbook.error if workbook else None
        one = units[0] if len(units) == 1 else {}
        changed = (actual is not None and actual.source_revision_id != source.revision_id) or any(
            unit["successful_revision_id"]
            and unit["successful_revision_id"] != unit["target_revision_id"]
            for unit in units
        )
        documents.append(
            {
                "hash": source.source_id,
                "source_id": source.source_id,
                "source_generation": source.generation,
                "legacy_hash": None,
                "location": source.source_key,
                "name": source.label,
                "type": PurePosixPath(source.label).suffix.lstrip("."),
                "original_kind": "managed",
                "source_revision_id": actual.source_revision_id if actual else source.revision_id,
                "target_source_revision_id": source.revision_id,
                "publication_id": bound.publication_id,
                "view_id": scope.view_id,
                "doc_name": targets[0].doc_name if targets else source.label,
                "unit_count": len(units),
                "tokens": one.get("tokens"),
                "pages": one.get("pages"),
                "characters": one.get("characters"),
                "units": units,
                "removed": source.removed,
                "contribution_empty": source.contribution_empty,
                "validity": "withdrawn"
                if source.removed
                else "empty"
                if source.contribution_empty
                else "needs_refresh"
                if changed
                else "current",
                "status": "removed"
                if source.removed
                else "empty"
                if source.contribution_empty
                else "failed"
                if inventory_error
                else source_status([SimpleNamespace(status=unit["status"]) for unit in units]),
                "message": inventory_error,
                "length_class": one.get("length_class"),
                "execution_mode": one.get("execution_mode"),
                "processing": one.get("processing"),
                "target_processing": one.get("target_processing"),
                "display_type": "pageindex"
                if one.get("execution_mode") == "segmented"
                else "short",
            }
        )
    return documents


def knowledge_inventory(scope):
    files = scope.knowledge.files()
    documents = source_inventory(scope)
    values = {
        "publication_id": scope.publication_id,
        "documents": documents,
        "document_count": len(documents),
    }
    for section in ("summaries", "concepts", "entities", "reports"):
        values[section] = sorted(
            PurePosixPath(path).name if section == "reports" else PurePosixPath(path).stem
            for path in files
            if path.startswith(section + "/") and path.endswith(".md")
        )
    return values
