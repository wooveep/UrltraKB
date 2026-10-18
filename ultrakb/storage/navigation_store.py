"""Prepare deterministic knowledge navigation before a publication can activate."""

import hashlib
import json

from pydantic import TypeAdapter

from ultrakb.context.navigation_nodes import compact_navigation, source_nodes
from ultrakb.source_records import RecordId

from .catalog_store import CatalogStore
from .index_store import IndexStore
from .knowledge_inputs import source_paths, source_selector
from .tree_store import TreeRevision, TreeStore, retain_tree

NAVIGATION_POLICY = "ultrakb-knowledge-forest-v3"


class RecoveryRequired(ValueError):
    """An explicit repair must restore a missing or inconsistent committed object."""

    code = "recovery_required"


def resolve_inputs(db, unit_revisions, index_revisions):
    """Each publication selects exactly one processing revision and index per unit."""
    if not isinstance(unit_revisions, tuple):
        raise ValueError("Knowledge requires distinct immutable processing unit identities")
    for unit_id in unit_revisions:
        TypeAdapter(RecordId).validate_python(unit_id)
    if len(set(unit_revisions)) != len(unit_revisions):
        raise ValueError("Knowledge requires distinct immutable processing unit identities")
    indexes = {}
    for index_id in index_revisions:
        row = db.connection.execute(
            "SELECT i.unit_revision_id FROM ukb_index_revisions i JOIN ukb_tree_builds t "
            "ON t.tree_id = i.tree_id WHERE i.index_revision_id = ? AND t.state = 'ready' "
            "AND t.protocol = 'document'",
            (index_id,),
        ).fetchone()
        if not row:
            raise ValueError("Knowledge requires complete accepted document indexes")
        if row[0] in indexes:
            raise ValueError("A processing unit cannot select multiple document indexes")
        indexes[row[0]] = index_id
    inputs = [
        (revision, indexes.get(revision))
        for revision in sorted(set(unit_revisions) | indexes.keys())
    ]
    units = set()
    for revision_id, _ in inputs:
        row = db.connection.execute(
            "SELECT r.unit_id FROM ukb_unit_revisions r JOIN ukb_processing_versions p "
            "ON p.processing_id = r.processing_id "
            "WHERE r.unit_revision_id = ? AND p.state = 'ready'",
            (revision_id,),
        ).fetchone()
        if not row or row[0] in units:
            raise ValueError("Knowledge requires one accepted processing revision per unit")
        units.add(row[0])
    return inputs


def prepare_navigation(
    root, identity, revision_id, publication_id, files, inputs, *, primary_units
) -> TreeRevision:
    # No execution model or endpoint participates in this deterministic identity.
    encoded = json.dumps(
        {
            "policy": NAVIGATION_POLICY,
            "revision": revision_id,
            "publication": publication_id,
            "files": files,
            "inputs": inputs,
            "primary_units": sorted(primary_units),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    catalog, indexes = CatalogStore(root, identity), IndexStore(root, identity)
    documents = {}
    for unit_revision_id, index_id in inputs:
        unit = catalog.unit_revision(unit_revision_id)
        retained = unit_revision_id not in primary_units
        processing = catalog.processing(unit.processing_id)
        file = next(file for file in processing.files if file.path == processing.source_path)
        reference = {
            "doc_name": source_selector(unit, retained=retained),
            "path": source_paths(unit, processing, retained=retained)[-1][0],
            "processing_path": file.path,
            "content_digest": file.digest,
            "source_id": unit.source_id,
            "source_revision_id": unit.source_revision_id,
            "processing_id": unit.processing_id,
            "unit_id": unit.unit_id,
            "unit_revision_id": unit_revision_id,
            "knowledge_revision_id": revision_id,
        }
        structure = indexes.document(index_id)["structure"] if index_id else []
        nodes = source_nodes(
            catalog.read_processed(unit.processing_id, file.path),
            processing.source_kind,
            reference,
            structure,
        )
        if nodes:
            documents[unit_revision_id] = compact_navigation(
                {
                    "title": unit.doc_name,
                    "rel_path": unit.doc_name,
                    "doc_name": unit.doc_name,
                    "is_dir": True,
                    "children": nodes,
                }
            )
    return TreeStore(root, identity).build(
        {"title": "Knowledge navigation", "is_dir": True, "children": documents},
        protocol="generic",
        input_fingerprint=fingerprint,
        meta={"source": "filesystem", "policy": NAVIGATION_POLICY},
    )


def bind_navigation(connection, revision_id, tree):
    """Bind inside the owning revision/repair transaction, retaining the ready tree."""
    changed = connection.execute(
        "INSERT INTO ukb_revision_navigation VALUES (?, ?, ?) "
        "ON CONFLICT(revision_id) DO UPDATE SET tree_id = excluded.tree_id "
        "WHERE input_fingerprint = excluded.input_fingerprint",
        (revision_id, tree.tree_id, tree.input_fingerprint),
    ).rowcount
    if changed != 1:
        raise ValueError("A navigation binding cannot change its committed input identity")
    connection.execute(
        "DELETE FROM ukb_tree_refs WHERE owner_kind = 'knowledge-revision' "
        "AND owner_id = ? AND slot = 'navigation'",
        (revision_id,),
    )
    retain_tree(connection, "knowledge-revision", revision_id, "navigation", tree.tree_id)


def require_navigation(db, revision_id) -> TreeRevision:
    row = db.connection.execute(
        "SELECT t.tree_id, t.protocol, t.input_fingerprint, t.manifest_digest "
        "FROM ukb_knowledge_revisions r JOIN ukb_revision_navigation n "
        "ON n.revision_id = r.revision_id AND n.input_fingerprint = r.navigation_fingerprint "
        "JOIN ukb_tree_builds t ON t.tree_id = n.tree_id "
        "AND t.input_fingerprint = n.input_fingerprint "
        "JOIN ukb_tree_refs ref ON ref.owner_kind = 'knowledge-revision' "
        "AND ref.owner_id = r.revision_id AND ref.slot = 'navigation' AND ref.tree_id = t.tree_id "
        "WHERE r.revision_id = ? AND t.state = 'ready' AND t.protocol = 'generic'",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise RecoveryRequired("Knowledge navigation requires explicit recovery")
    return TreeRevision(*row)
