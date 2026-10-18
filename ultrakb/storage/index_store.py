"""Accepted document indexes bind fixed processing units and reusable ready trees."""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from ultrakb.source_records import Digest, RecordId

from .coordination import storage_stage
from .database import DatabaseIdentity
from .tree_store import TreeStore, retain_tree


@dataclass(frozen=True)
class IndexRevision:
    index_revision_id: str
    unit_revision_id: str
    tree_id: str
    content_fingerprint: str


class IndexStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def find(self, unit_revision_id: str, *, content_fingerprint: str) -> IndexRevision | None:
        TypeAdapter(RecordId).validate_python(unit_revision_id)
        TypeAdapter(Digest).validate_python(content_fingerprint)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT i.index_revision_id, i.unit_revision_id, i.tree_id, i.content_fingerprint "
                "FROM ukb_index_revisions i JOIN ukb_tree_builds t ON t.tree_id = i.tree_id "
                "WHERE i.unit_revision_id = ? AND i.content_fingerprint = ? "
                "AND t.state = 'ready' AND t.protocol = 'document'",
                (unit_revision_id, content_fingerprint),
            ).fetchone()
            return IndexRevision(*row) if row else None

    def accept(self, unit_revision_id: str, document: dict, *, content_fingerprint: str):
        previous = self.find(unit_revision_id, content_fingerprint=content_fingerprint)
        if previous:
            return previous
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if not db.connection.execute(
                "SELECT 1 FROM ukb_unit_revisions WHERE unit_revision_id = ?", (unit_revision_id,)
            ).fetchone():
                raise KeyError(unit_revision_id)
        # The build commits independently; publication or binding failure cannot undo it.
        with storage_stage(self.root, self.identity) as db:
            tree = TreeStore(self.root, self.identity).build(
                document, protocol="document", input_fingerprint=content_fingerprint
            )
            # Keep protection from the last build access through its durable binding.
            with db.transaction() as connection:
                index_id = uuid4().hex
                inserted = connection.execute(
                    "INSERT INTO ukb_index_revisions VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(unit_revision_id, content_fingerprint) DO NOTHING",
                    (index_id, unit_revision_id, tree.tree_id, content_fingerprint),
                ).rowcount
                if inserted:
                    retain_tree(connection, "index-revision", index_id, "document", tree.tree_id)
                row = connection.execute(
                    "SELECT index_revision_id, unit_revision_id, tree_id, content_fingerprint "
                    "FROM ukb_index_revisions "
                    "WHERE unit_revision_id = ? AND content_fingerprint = ?",
                    (unit_revision_id, content_fingerprint),
                ).fetchone()
                return IndexRevision(*row)

    def revision(self, index_revision_id: str) -> IndexRevision:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT i.index_revision_id, i.unit_revision_id, i.tree_id, i.content_fingerprint "
                "FROM ukb_index_revisions i JOIN ukb_tree_builds t ON t.tree_id = i.tree_id "
                "WHERE i.index_revision_id = ? AND t.state = 'ready' AND t.protocol = 'document'",
                (index_revision_id,),
            ).fetchone()
            if not row:
                raise KeyError(index_revision_id)
            return IndexRevision(*row)

    def document(self, index_revision_id: str) -> dict:
        selected = self.revision(index_revision_id)
        with TreeStore(self.root, self.identity).open(selected.tree_id) as tree:
            return tree.content(tree.root_id)["document"]
