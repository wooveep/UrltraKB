"""Seal and copy index packages through the existing filesystem publication."""

from pathlib import Path

from openkb.condb_storage import ConDBPageIndexStorage
from openkb.index_location import IndexLocation
from openkb.locks import atomic_write_json


def _validated(root: Path, document_id: str | None = None) -> IndexLocation:
    location = IndexLocation.package(root)
    if not location.database.is_file():
        raise FileNotFoundError("Missing document index database")
    # Close/checkpoint before copying; never initialize a missing source package.
    with ConDBPageIndexStorage(location) as storage:
        if document_id and not storage.get_document("default", document_id):
            raise ValueError("Index package does not contain its referenced document")
    return location


def _write_location(root: Path, location: IndexLocation) -> None:
    atomic_write_json(
        root / "index.json",
        {
            "format": location.format,
            "database": location.database.relative_to(root.resolve()).as_posix(),
            "inputs_root": location.inputs_root.relative_to(root.resolve()).as_posix(),
            "read_only": location.read_only,
            "owner_revision": location.owner_revision,
        },
    )


def seal_index_package(root: Path, owner_revision: str) -> IndexLocation:
    location = _validated(root)
    if location.read_only and location.owner_revision != owner_revision:
        raise ValueError("Cannot change a sealed index's revision owner")
    sealed = IndexLocation(
        location.database, location.inputs_root, read_only=True, owner_revision=owner_revision
    )
    if not location.read_only:
        _write_location(root, sealed)
    return sealed


def copy_index_package(
    source: Path, target: Path, *, owner_revision: str | None = None, document_id: str | None = None
) -> None:
    from openkb.mutation import _copy_file_atomic
    from openkb.unit_publication import copy_tree

    location = _validated(source, document_id)
    owner = owner_revision or location.owner_revision
    copied = IndexLocation(
        target / "context.sqlite",
        target / "files",
        read_only=bool(owner) or location.read_only,
        owner_revision=owner,
    )
    _copy_file_atomic(location.database, copied.database)
    if location.inputs_root.exists():
        copy_tree(location.inputs_root, copied.inputs_root)
    _write_location(target, copied)
