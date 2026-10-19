"""Managed source targets alongside their exact fixed-publication knowledge basis."""

from pathlib import PurePosixPath

from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.pending_store import PendingStore


def catalog_sources(catalog):
    after = ""
    while sources := catalog.source_page(after=after):
        yield from sources
        after = sources[-1].source_id


def _unit(catalog, target, actual, scope):
    processing = catalog.processing(target.processing_id)
    used = catalog.processing(actual.processing_id) if actual else None
    decision = used.decision if used else None
    content_state = processing.content_state
    complete = actual is not None and actual.unit_revision_id == target.unit_revision_id
    status = (
        "completed"
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
    return {
        "unit_id": target.unit_id,
        "key": target.key,
        "name": target.doc_name,
        "doc_name": target.doc_name,
        "target_revision_id": target.unit_revision_id,
        "target_source_revision_id": target.source_revision_id,
        "successful_revision_id": actual.unit_revision_id if actual else None,
        "successful_source_revision_id": actual.source_revision_id if actual else None,
        "knowledge_revision_id": scope.knowledge.publication.revision_id if actual else None,
        "view_id": scope.view_id,
        "status": status,
        "content_state": content_state,
        "error_type": work[1] if work else None,
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
    for source in catalog_sources(catalog):
        targets = catalog.source_units(source.source_id)
        units = [_unit(catalog, unit, actuals.get(unit.unit_id), scope) for unit in targets]
        actual = next((actuals[unit.unit_id] for unit in targets if unit.unit_id in actuals), None)
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
                "units": units,
                "validity": "needs_refresh" if changed else "current",
                "status": source_status([SimpleNamespace(status=unit["status"]) for unit in units]),
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
