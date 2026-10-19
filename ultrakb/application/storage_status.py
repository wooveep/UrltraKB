"""Describe a fixed committed knowledge inventory without inspecting a live worktree."""

from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.knowledge_validity import publication_validity


def knowledge_status(scope):
    bound = scope.knowledge
    validity = publication_validity(bound)
    with storage_stage(bound.root, bound.identity, read_only=True) as db:
        db.connection.execute("BEGIN")
        bound._require_publication(db)
        counts = dict(
            db.connection.execute(
                "SELECT substr(path, 1, instr(path, '/') - 1), COUNT(*) "
                "FROM ukb_revision_files WHERE revision_id = ? AND path LIKE '%.md' "
                "AND instr(path, '/') > 0 AND instr(substr(path, instr(path, '/') + 1), '/') = 0 "
                "GROUP BY 1",
                (bound.publication.revision_id,),
            ).fetchall()
        )
        [source_count] = db.connection.execute("SELECT COUNT(*) FROM ukb_sources").fetchone()
        [published_at] = db.connection.execute(
            "SELECT published_at FROM ukb_publication_receipts WHERE publication_id = ?",
            (bound.publication_id,),
        ).fetchone()
        # Receipts are append-only and publication is a CAS-guarded linear chain
        # per view. Internal row order also works when the wall clock moves back.
        compiled = db.connection.execute(
            "SELECT r.published_at FROM ukb_publication_receipts r "
            "LEFT JOIN ukb_revision_import i ON i.revision_id = r.revision_id "
            "LEFT JOIN ukb_revision_refresh f ON f.revision_id = r.revision_id "
            "JOIN ukb_publications p ON p.publication_id = r.publication_id "
            "WHERE p.view_id = ? AND (i.revision_id IS NOT NULL OR "
            "(f.revision_id IS NOT NULL AND json_extract(f.outcome_json, '$.quality_known') = 1)) "
            "AND r.rowid <= "
            "(SELECT rowid FROM ukb_publication_receipts WHERE publication_id = ?) "
            "ORDER BY r.rowid DESC LIMIT 1",
            (scope.view_id, bound.publication_id),
        ).fetchone()
        linted = db.connection.execute(
            "SELECT r.published_at FROM ukb_publication_receipts r "
            "JOIN ukb_lint_candidates l ON l.revision_id = r.revision_id "
            "JOIN ukb_publications p ON p.publication_id = r.publication_id "
            "WHERE p.view_id = ? AND r.rowid <= "
            "(SELECT rowid FROM ukb_publication_receipts WHERE publication_id = ?) "
            "ORDER BY r.rowid DESC LIMIT 1",
            (scope.view_id, bound.publication_id),
        ).fetchone()
    reasons = validity["needs_refresh"]
    return {
        "publication_id": bound.publication_id,
        "directories": {
            section: counts.get(section, 0)
            for section in ("sources", "summaries", "concepts", "entities", "reports")
        },
        "needs_refresh": {scope.view_id: reasons} if reasons else {},
        "raw_count": source_count,
        "total_indexed": source_count,
        "published_at": published_at,
        "last_compile": compiled[0] if compiled else None,
        "last_compile_availability": "known" if compiled else "unrecorded",
        "last_lint": linted[0] if linted else None,
        "last_lint_availability": "known" if linted else "unrecorded",
    }
