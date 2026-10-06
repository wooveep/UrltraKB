"""Explicit current-format state for tests that assemble minimal KBs by hand."""

import json


def mark_current_kb(root):
    from openkb.kb_format import KB_FORMAT

    state = root / ".openkb"
    state.mkdir(parents=True, exist_ok=True)
    (state / "format.json").write_text(json.dumps(KB_FORMAT))


def seed_document_index(root, identity, name):
    from test_condb_pageindex import indexed_document

    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.index_location import IndexLocation

    with ConDBPageIndexStorage(IndexLocation.package(root / ".openkb")) as storage:
        storage.get_or_create_collection("default")
        storage.save_document(
            "default",
            identity,
            indexed_document(
                storage.location.inputs_root, identity=identity, name=name, key=identity
            ),
        )
