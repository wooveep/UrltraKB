"""Explicit current-format state for tests that assemble minimal KBs by hand."""


def mark_current_kb(root):
    """Explicitly create an empty native test store; future file edits stay drafts."""
    from ultrakb.storage.database import create_database, database_path, open_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    if database_path(root).is_file():
        with open_database(root, read_only=True):
            return
    # Some callers assembled only configuration before explicitly requesting
    # a native fixture. Preserve that input, never interpret an old registry.
    import os
    import tempfile
    from pathlib import Path

    state = root / ".ultrakb"
    entries = {path.name for path in state.iterdir()} if state.exists() else set()
    if entries - {"config.yaml"}:
        raise ValueError("Native test fixtures cannot migrate existing state")
    with tempfile.TemporaryDirectory(prefix="native-fixture-config-", dir=root.parent) as name:
        config = state / "config.yaml"
        saved = Path(name) / "config.yaml"
        if config.exists():
            os.replace(config, saved)
        try:
            identity = create_database(root)
        finally:
            if saved.exists():
                os.replace(saved, config)
    if not config.exists():
        from ultrakb.config import save_config

        save_config(config, {})
    store = KnowledgeStore(root, identity)
    store.publish(store.prepare_revision({}, expected=None))


def seed_document_index(root, identity, name):
    from test_condb_pageindex import indexed_document

    from ultrakb.condb_storage import ConDBPageIndexStorage
    from ultrakb.index_location import IndexLocation

    with ConDBPageIndexStorage(IndexLocation.package(root / ".ultrakb")) as storage:
        storage.get_or_create_collection("default")
        storage.save_document(
            "default",
            identity,
            indexed_document(
                storage.location.inputs_root, identity=identity, name=name, key=identity
            ),
        )
