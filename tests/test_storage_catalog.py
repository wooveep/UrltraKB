"""New-format development factory: no legacy files or migration path."""


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
