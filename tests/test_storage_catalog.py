"""New-format development factory: no legacy files or migration path."""


def test_worksheet_targets_have_independent_identity_and_processing_history(tmp_path):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database

    root = tmp_path / "kb"
    catalog = CatalogStore(root, create_database(root))
    source = tmp_path / "workbook.xlsx"
    source.write_bytes(b"Workbook source witness")
    revision = catalog.admit(source, source_key="workbook")
    body = tmp_path / "sheet.md"
    body.write_text("Sheet contents")

    def processed(operation):
        return catalog.retain_processing(
            revision.revision_id,
            {"sheet.md": body},
            body_path="sheet.md",
            fingerprint="worksheet-v1",
            operation_id=operation,
            decision=classify_markdown_tokens(2),
        )

    sales_processing, costs_processing = processed("sales-one"), processed("costs-one")
    sales = catalog.plan_unit(sales_processing.processing_id, key="sheet-sales", doc_name="sales")
    costs = catalog.plan_unit(costs_processing.processing_id, key="sheet-costs", doc_name="costs")
    body.write_text("Reprocessed sales contents")
    sales_next = catalog.plan_unit(
        processed("sales-two").processing_id,
        key="sheet-sales",
        doc_name="sales",
        expected_generation=sales.generation,
    )
    assert sales.unit_id != costs.unit_id
    assert sales_next.unit_id == sales.unit_id
    assert sales_next.unit_revision_id != sales.unit_revision_id
    assert catalog.unit(costs.unit_id) == costs
    assert catalog.unit_revision(sales.unit_revision_id) == sales


def test_distinct_sources_share_bytes_and_survive_external_deletion(tmp_path):
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database

    root = tmp_path / "kb"
    identity = create_database(root)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# Retained original\n\nOne shared payload.\n")
    second.write_bytes(first.read_bytes())
    catalog = CatalogStore(root, identity)
    left = catalog.admit(first, source_key="first")
    right = catalog.admit(second, source_key="second")
    first.unlink()
    second.unlink()

    reopened = CatalogStore(root, identity)
    assert left.source_id != right.source_id
    assert left.revision_id != right.revision_id
    assert left.files[0].digest == right.files[0].digest
    assert reopened.read_original(left.revision_id) == (
        b"# Retained original\n\nOne shared payload.\n"
    )
    assert reopened.read_original(right.revision_id) == reopened.read_original(left.revision_id)


def test_original_filename_does_not_collide_with_manifest_identity(tmp_path):
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database

    root = tmp_path / "kb"
    catalog = CatalogStore(root, create_database(root))
    source = tmp_path / "manifest"
    source.write_text("A legitimate original file without an extension.")
    revision = catalog.admit(source, source_key="original")
    assert catalog.read_original(revision.revision_id) == source.read_bytes()


def test_admission_reuses_exact_managed_input_and_keeps_changed_sidecars_separate(tmp_path):
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database

    root = tmp_path / "kb"
    catalog = CatalogStore(root, create_database(root))
    source, sidecar = tmp_path / "book.md", tmp_path / "image.png"
    source.write_text("# Book\n\n![Figure](assets/image.png)")
    sidecar.write_bytes(b"first figure")
    first = catalog.admit(source, source_key="book", sidecars={"assets/image.png": sidecar})
    repeated = catalog.admit(source, source_key="book", sidecars={"assets/image.png": sidecar})
    assert repeated == first
    sidecar.write_bytes(b"revised figure")
    changed = catalog.admit(source, source_key="book", sidecars={"assets/image.png": sidecar})
    source.unlink()
    sidecar.unlink()
    assert changed.source_id == first.source_id
    assert changed.revision_id != first.revision_id
    assert catalog.read_file(first.revision_id, "assets/image.png") == b"first figure"
    assert catalog.read_file(changed.revision_id, "assets/image.png") == b"revised figure"


def test_processing_versions_preserve_original_and_previously_accepted_results(tmp_path):
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.database import create_database

    root = tmp_path / "kb"
    catalog = CatalogStore(root, create_database(root))
    source, converted = tmp_path / "manual.txt", tmp_path / "body.md"
    source.write_bytes(b"Original managed input")
    original = catalog.admit(source, source_key="manual")
    converted.write_text("First accepted interpretation")
    options = dict(
        body_path="body.md",
        fingerprint="parser-v1",
        operation_id="processing-one",
        decision=classify_markdown_tokens(4),
    )
    first = catalog.retain_processing(original.revision_id, {"body.md": converted}, **options)
    assert (
        catalog.retain_processing(original.revision_id, {"body.md": converted}, **options) == first
    )
    converted.write_text("Second accepted interpretation")
    second = catalog.retain_processing(
        original.revision_id,
        {"body.md": converted},
        body_path="body.md",
        fingerprint="parser-v2",
        operation_id="processing-two",
        decision=classify_markdown_tokens(4),
    )
    source.unlink()
    converted.unlink()
    assert first.processing_id != second.processing_id
    assert first.source_revision_id == second.source_revision_id == original.revision_id
    assert catalog.read_original(original.revision_id) == b"Original managed input"
    assert (
        catalog.read_processed(first.processing_id, "body.md") == b"First accepted interpretation"
    )
    assert (
        catalog.read_processed(second.processing_id, "body.md") == b"Second accepted interpretation"
    )
