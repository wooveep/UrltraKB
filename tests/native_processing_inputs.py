"""Retain actual physical slide pages for reading before semantic index generation."""


def retain_physical_slide_pages(kb_dir, source_id):
    from ultrakb.images import convert_pdf_to_pages
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.locks import atomic_write_json, kb_ingest_lock
    from ultrakb.mutation import mutation_scope
    from ultrakb.normalization import normalization_id, read_normalization
    from ultrakb.office.slide_content import attach_slides, read_slides
    from ultrakb.source_catalog import read_record, record_path, write_record
    from ultrakb.unit_publication import list_source_units, wiki_versions

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        [unit] = list_source_units(kb_dir, source_id)
        revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
        identity = normalization_id(revision.source_revision_id, revision.processing_fingerprint)
        directory, normalized = read_normalization(kb_dir, identity)
        assert normalized.pdf_path and normalized.office_path
        target = f"wiki/sources/{unit.doc_name}.json"
        if target in normalized.files:
            return  # The short conversion already retained these physical pages.

        # Derive only the frozen original's page bodies, images, body/notes and
        # physical coordinates. No semantic tree, title or model result is built.
        record = record_path(kb_dir, "normalizations", identity)
        with mutation_scope(kb_dir, [directory, record], operation="retain-native-page-inputs"):
            pages = convert_pdf_to_pages(
                directory / normalized.pdf_path,
                unit.doc_name,
                directory / f"wiki/sources/images/{unit.doc_name}",
            )
            pages = attach_slides(pages, read_slides(directory / normalized.office_path))
            atomic_write_json(directory / target, pages)
            files = wiki_versions(kb_dir, directory)
            assert all(files[path] == digest for path, digest in normalized.files.items())
            write_record(record, normalized.model_copy(update={"files": files}))
        read_normalization(kb_dir, identity)  # Validate the real artifact relationships.
