"""Read genuine native catalog and CAS evidence in acceptance fixtures."""

from ultrakb.application.sources import source_inventory
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.database import open_database


def catalog(root):
    with open_database(root, read_only=True) as db:
        return CatalogStore(root, db.identity)


def list_sources(root):
    store = catalog(root)
    return [store.source(item["source_id"]) for item in source_inventory(root)]


def read_source(root, source_id):
    return catalog(root).source(source_id)


def read_source_revision(root, revision_id):
    return catalog(root).revision(revision_id)


def list_source_units(root, source_id):
    return catalog(root).source_units(source_id)


def original_bytes(root, revision_id):
    return catalog(root).read_original(revision_id)


def publication(root, view_id="legacy"):
    from ultrakb.application.views import published_scope

    return published_scope(root, view_id).knowledge.publication


def published_files(root):
    from ultrakb.application.views import published_scope

    return published_scope(root).knowledge.files()


def unit_inventory(root, unit_id, view_id="legacy"):
    from ultrakb.application.views import published_scope

    return next(
        unit
        for source in source_inventory(root, scope=published_scope(root, view_id))
        for unit in source["units"]
        if unit["unit_id"] == unit_id
    )


def document_name(root, label):
    [source] = [source for source in list_sources(root) if source.label == label + ".md"]
    [unit] = catalog(root).source_units(source.source_id)
    return unit.doc_name


def retained_image_bytes(root, reader, path):
    from ultrakb.application.storage_sources import read_source_image

    return read_source_image(
        root, reader["source_id"], reader["unit_revision_id"], path, identity=catalog(root).identity
    )[0]


def processing_snapshot(root, unit_revision_ids=None):
    """Read immutable native processing metadata and verify its actual CAS bytes."""
    store = catalog(root)
    if unit_revision_ids is None:
        unit_revision_ids = [
            unit.unit_revision_id
            for source in list_sources(root)
            for unit in store.source_units(source.source_id)
        ]
    result = {}
    for revision_id in unit_revision_ids:
        unit = store.unit_revision(revision_id)
        processing = store.processing(unit.processing_id)
        contents = tuple(
            (file.path, store.read_processed(processing.processing_id, file.path))
            for file in processing.files
        )
        result[revision_id] = (unit, processing, contents)
    return result


def retained_pdf_bytes(root, reader):
    store = catalog(root)
    target = store.unit_revision(reader["unit_revision_id"])
    processing = store.processing(target.processing_id)
    [pdf] = [
        file
        for file in processing.files
        if file.path.startswith("conversion/") and file.path.endswith(".pdf")
    ]
    return store.read_processed(processing.processing_id, pdf.path)


def knowledge_forest(selection):
    """Inspect the genuine fixed native navigation without a private SQL oracle."""
    bound = selection.views[0].scope.knowledge
    with bound.navigation() as tree:

        def node(value):
            return {
                **value["attrs"],
                "children": {
                    child["node_id"]: node(child) for child in tree.children(value["node_id"])
                },
            }

        return node(tree.node(tree.root_id))
