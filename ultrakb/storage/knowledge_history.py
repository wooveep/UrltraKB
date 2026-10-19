"""Bounded committed history, ordered by receipt rather than mutable worktree state."""

from pydantic import TypeAdapter

from ultrakb.source_records import RecordId

from .coordination import storage_stage
from .knowledge_store import KnowledgeStore
from .wiki_paths import wiki_path


def require_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("History requires a bounded limit of 1..128")


class KnowledgeHistory:
    def __init__(self, base):
        self.base = base

    def _sequence(self, db, publication_id):
        row = db.connection.execute(
            "SELECT c.rowid FROM ukb_publication_receipts c JOIN ukb_publications p "
            "ON p.publication_id = c.publication_id AND p.revision_id = c.revision_id "
            "WHERE p.publication_id = ? AND p.view_id = ?",
            (publication_id, self.base.publication.view_id),
        ).fetchone()
        if row is None:
            raise FileNotFoundError("No committed history in this knowledge view")
        return row[0]

    def list(self, *, after=None, limit=63):
        require_limit(limit)
        if after is not None:
            TypeAdapter(RecordId).validate_python(after)
        with storage_stage(self.base.root, self.base.identity, read_only=True) as db:
            db.connection.execute("BEGIN")
            self.base._require_publication(db)
            upper = self._sequence(db, self.base.publication_id)
            cursor = self._sequence(db, after) if after is not None else upper + 1
            if cursor > upper and after is not None:
                raise ValueError("History cursor is newer than its selected publication")
            rows = db.connection.execute(
                "SELECT p.publication_id, p.revision_id, parent.revision_id, c.published_at, "
                "CASE WHEN i.revision_id IS NOT NULL THEN 'compile' "
                "WHEN f.revision_id IS NOT NULL THEN 'refresh' "
                "WHEN s.revision_id IS NOT NULL THEN s.kind ELSE 'manual' END "
                "FROM ukb_publication_receipts c JOIN ukb_publications p "
                "ON p.publication_id = c.publication_id AND p.revision_id = c.revision_id "
                "JOIN ukb_knowledge_revisions r ON r.revision_id = p.revision_id "
                "LEFT JOIN ukb_publications parent "
                "ON parent.publication_id = r.expected_publication "
                "LEFT JOIN ukb_revision_import i ON i.revision_id = r.revision_id "
                "LEFT JOIN ukb_revision_refresh f ON f.revision_id = r.revision_id "
                "LEFT JOIN ukb_revision_source_changes s ON s.revision_id = r.revision_id "
                "WHERE p.view_id = ? AND c.rowid <= ? AND c.rowid < ? "
                "ORDER BY c.rowid DESC LIMIT ?",
                (self.base.publication.view_id, upper, cursor, limit),
            ).fetchall()
        return tuple(
            {
                "publication_id": row[0],
                "knowledge_revision_id": row[1],
                "base_revision_id": row[2],
                "published_at": row[3],
                "change_kind": row[4],
                "history_publication_id": self.base.publication_id,
            }
            for row in rows
        )

    def revision(self, revision_id):
        TypeAdapter(RecordId).validate_python(revision_id)
        with storage_stage(self.base.root, self.base.identity, read_only=True) as db:
            db.connection.execute("BEGIN")
            self.base._require_publication(db)
            upper = self._sequence(db, self.base.publication_id)
            row = db.connection.execute(
                "SELECT p.publication_id FROM ukb_publications p JOIN ukb_publication_receipts c "
                "ON c.publication_id = p.publication_id AND c.revision_id = p.revision_id "
                "WHERE p.revision_id = ? AND p.view_id = ? AND c.rowid <= ?",
                (revision_id, self.base.publication.view_id, upper),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("Requested knowledge history is unavailable")
        return KnowledgeStore(self.base.root, self.base.identity).bind(
            row[0], view_id=self.base.publication.view_id
        )

    def pages(self, revision_id, *, after=None, limit=63):
        require_limit(limit)
        if after is not None:
            wiki_path(after)
        selected = self.revision(revision_id)
        with storage_stage(selected.root, selected.identity, read_only=True) as db:
            selected._require_publication(db)
            return tuple(
                row[0]
                for row in db.connection.execute(
                    "SELECT path FROM ukb_revision_files WHERE revision_id = ? AND path > ? "
                    "AND path GLOB '*.md' AND path NOT IN ('AGENTS.md', 'SCHEMA.md', 'log.md') "
                    "ORDER BY path LIMIT ?",
                    (revision_id, after or "", limit),
                ).fetchall()
            )
