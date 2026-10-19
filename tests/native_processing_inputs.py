"""Validate physical slide input already retained by the native processing pipeline."""

import json


def retain_physical_slide_pages(kb_dir, source_id):
    from native_catalog_fixtures import catalog

    from ultrakb.source_pages import read_page_selection

    store = catalog(kb_dir)
    [unit] = store.source_units(source_id)
    processing = store.processing(unit.processing_id)
    assert processing.source_kind == "pages"
    pages = store.read_processed(processing.processing_id, processing.source_path)
    read_page_selection(json.loads(pages))
