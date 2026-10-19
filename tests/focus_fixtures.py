"""Explicit native partial-workbook and retained-original fixtures, without a model."""

from openpyxl import Workbook
from processing_fixtures import source_processing_context

from ultrakb.application.documents import import_document
from ultrakb.application.views import published_scope
from ultrakb.locks import atomic_write_text
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.pending_store import PendingStore


def partial_workbook(kb, path, *, bundle):
    from native_publication_fixtures import publication_workspace

    from ultrakb.agent.compiler import _write_summary
    from ultrakb.selected_refresh import _refresh_index
    from ultrakb.storage.knowledge_inputs import source_paths

    workbook = Workbook()
    workbook.active.title = "Alpha"
    workbook.active["A1"] = "Available review policy"
    for name in ("Beta", "Gamma"):
        workbook.create_sheet(name)["A1"] = f"Unavailable {name} policy"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    imported = import_document(kb, path, context=source_processing_context(bundle=bundle))
    assert imported.source_id is not None, imported
    catalog = CatalogStore(kb, published_scope(kb).knowledge.identity)
    from ultrakb.application.sources import source_inventory

    source = next(item for item in source_inventory(kb) if item["source_id"] == imported.source_id)
    units = {
        item["name"]: catalog.unit_revision(item["target_revision_id"]) for item in source["units"]
    }
    assert "Alpha" in units, source
    alpha = units["Alpha"]
    processing = catalog.processing(alpha.processing_id)
    body = catalog.read_processed(processing.processing_id, processing.body_path).decode()
    with publication_workspace(kb, alpha.unit_revision_id) as (wiki, target):
        _write_summary(
            wiki,
            target.doc_name,
            body,
            description="Available review policy",
            full_text=source_paths(target, processing)[-1][0],
        )
        _refresh_index(wiki)
    beta = units["Beta"]
    pending = PendingStore(kb, catalog.identity)
    work = pending.enqueue(beta.source_revision_id, unit_revision_id=beta.unit_revision_id)
    claim = pending.start(pending.claim(intent_id=work.intent_id))
    pending.finish(claim, status="failed", error_code="LocalWorksheetDeliveryFailed")
    return imported.source_id


def unprocessed_source(kb, path):
    atomic_write_text(path, "# Retained original\n\nNo normalized body yet.")
    bound = published_scope(kb).knowledge
    return CatalogStore(kb, bound.identity).admit(path, source_key=str(path.resolve())).source_id
