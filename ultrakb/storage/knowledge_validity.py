"""Compare an immutable page basis with catalog targets in one short read snapshot."""

from .coordination import storage_stage
from .wiki_paths import wiki_path


def _inputs(db, bound, *, page=None, native_unit=None):
    revision = bound.publication.revision_id
    parameters = (revision, revision)
    basis = (
        "SELECT unit_revision_id FROM ukb_revision_inputs WHERE revision_id = ? "
        "UNION SELECT unit_revision_id FROM ukb_revision_page_inputs WHERE revision_id = ?"
    )
    if native_unit is not None:
        basis = f"SELECT unit_revision_id FROM ({basis}) WHERE unit_revision_id = ?"
        parameters += (native_unit,)
    elif page is not None:
        basis = (
            "SELECT unit_revision_id FROM ukb_revision_page_inputs "
            "WHERE revision_id = ? AND path = ?"
        )
        parameters = (revision, page)
    return db.connection.execute(
        f"WITH basis AS ({basis}) "
        "SELECT b.unit_revision_id, u.unit_id, u.source_id, p.source_revision_id, "
        "s.state, s.generation, u.target_revision_id, "
        "(SELECT revision_id FROM ukb_source_revisions r WHERE r.source_id = s.source_id "
        "AND r.state = 'ready' ORDER BY r.ordinal DESC LIMIT 1), s.contribution_empty "
        "FROM basis b JOIN ukb_unit_revisions v ON v.unit_revision_id = b.unit_revision_id "
        "JOIN ukb_import_units u ON u.unit_id = v.unit_id "
        "JOIN ukb_processing_versions p ON p.processing_id = v.processing_id "
        "JOIN ukb_sources s ON s.source_id = u.source_id ORDER BY b.unit_revision_id",
        parameters,
    ).fetchall()


def _reason(row):
    revision, unit, source, actual, state, generation, target_unit, target_source, empty = row
    kind = (
        "withdrawn"
        if state == "withdrawn"
        else "empty"
        if empty
        else "updated"
        if revision != target_unit or actual != target_source
        else None
    )
    if kind is None:
        return None
    return {
        "source_id": source,
        "source_revision_id": actual,
        "target_revision_id": target_source,
        "source_generation": generation,
        "kind": kind,
        "unit_id": unit,
    }


def publication_validity(bound, *, page=None, native_unit=None):
    """Keep the selected bytes/basis fixed; report catalog validity separately.

    No body or original is opened, and no write/ingest lease is acquired. The read
    snapshot closes before any caller waits for a model or materializes resources.
    """
    if page is not None:
        wiki_path(page)
    with storage_stage(bound.root, bound.identity, read_only=True) as db:
        db.connection.execute("BEGIN")
        bound._require_publication(db)
        rows = _inputs(db, bound, page=page, native_unit=native_unit)
        reasons = {row[0]: reason for row in rows if (reason := _reason(row)) is not None}
        pending = {}
        if page is None:
            for path, revision in db.connection.execute(
                "SELECT path, unit_revision_id FROM ukb_revision_page_inputs "
                "WHERE revision_id = ? ORDER BY path, unit_revision_id",
                (bound.publication.revision_id,),
            ).fetchall():
                if revision in reasons:
                    pending.setdefault(path, []).append(reasons[revision])
        active = {
            row[0]
            for row in db.connection.execute(
                "SELECT unit_revision_id FROM ukb_revision_inputs WHERE revision_id = ?",
                (bound.publication.revision_id,),
            ).fetchall()
        }
        return {
            "view_id": bound.publication.view_id,
            "publication_id": bound.publication_id,
            "knowledge_revision_id": bound.publication.revision_id,
            "database_generation": bound.identity.generation,
            "validity": "needs_refresh" if reasons else "current",
            "source_revision_ids": tuple(sorted({row[3] for row in rows})),
            "refresh_reasons": tuple(reasons.values()),
            "needs_refresh": pending,
            "effective_inputs": tuple(sorted((active & {row[0] for row in rows}) - reasons.keys())),
        }
