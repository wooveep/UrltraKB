"""Capture source-selected pending work and its current accepted input identities."""

from dataclasses import dataclass

from pydantic import TypeAdapter

from ultrakb.compile_checkpoints import digest
from ultrakb.selected_refresh_evidence import selected_pages
from ultrakb.source_records import RecordId

from .catalog_store import CatalogStore
from .coordination import storage_stage
from .knowledge_store import PublicationConflict
from .knowledge_validity import _inputs, _reason


def selection_key(source_ids, page_paths):
    if not source_ids or len(set(source_ids)) != len(source_ids):
        raise ValueError("Select distinct problem source IDs")
    for source in source_ids:
        TypeAdapter(RecordId).validate_python(source)
    return digest(
        {
            "sources": sorted(source_ids),
            "pages": sorted(page_paths) if page_paths is not None else None,
        }
    )


@dataclass(frozen=True)
class SelectedTargets:
    generation: int
    source_ids: tuple[str, ...]
    page_paths: tuple[str, ...] | None
    units: tuple
    basis: dict
    old_units: dict
    stale_revisions: set
    pending: dict
    reasons: dict
    pages: set
    blocked: tuple

    @property
    def model_required(self):
        return any(self.may_reconcile(page) for page in self.pages)

    def may_reconcile(self, page):
        return page.startswith(("concepts/", "entities/")) and all(
            r["source_id"] in self.source_ids for r in self.pending[page]
        )


def selected_targets(bound, source_ids, page_paths=None):
    selection_key(source_ids, page_paths)
    with storage_stage(bound.root, bound.identity, read_only=True) as db:
        db.connection.execute("BEGIN")
        bound._require_publication(db)
        if db.connection.execute(
            "SELECT publication_id FROM ukb_heads WHERE view_id = ?",
            (bound.publication.view_id,),
        ).fetchone() != (bound.publication_id,):
            raise PublicationConflict("Knowledge changed before selected refresh")
        generation = db.connection.execute(
            "SELECT generation FROM ukb_catalog_state WHERE singleton = 1"
        ).fetchone()[0]
        labels = {}
        for source in source_ids:
            source_row = db.connection.execute(
                "SELECT state, contribution_empty, label FROM ukb_sources WHERE source_id = ?",
                (source,),
            ).fetchone()
            if source_row is None or source_row[:2] != ("active", 0):
                raise ValueError("Selected refresh requires active sources")
            labels[source] = source_row[2]
        rows = _inputs(db, bound)
        problems = {row[0]: reason for row in rows if (reason := _reason(row)) is not None}
        active = {
            row[0]
            for row in db.connection.execute(
                "SELECT unit_revision_id FROM ukb_revision_inputs WHERE revision_id = ?",
                (bound.publication.revision_id,),
            ).fetchall()
        }
        old_units = {row[0]: CatalogStore._unit_revision(db, row[0]) for row in rows}
        units = tuple(
            old_units[row[0]] for row in rows if row[0] in active and row[0] not in problems
        )
        basis, pending = {}, {}
        for path, unit in db.connection.execute(
            "SELECT path, unit_revision_id FROM ukb_revision_page_inputs WHERE revision_id = ? "
            "ORDER BY path, unit_revision_id",
            (bound.publication.revision_id,),
        ).fetchall():
            basis.setdefault(path, []).append(unit)
            if unit in problems:
                pending.setdefault(path, []).append(problems[unit])
        reasons = {
            path: tuple(r for r in values if r["source_id"] in source_ids)
            for path, values in pending.items()
            if any(r["source_id"] in source_ids for r in values)
        }
        if any(r["kind"] != "updated" for values in reasons.values() for r in values):
            raise ValueError("Selected refresh requires current updated-source reasons")
        pages = selected_pages(reasons, page_paths)
        missing = set(source_ids) - {unit.source_id for unit in units}
        blocked = (
            tuple(sorted(labels[source] for source in missing))
            if missing
            and (page_paths is None or any(not path.startswith("summaries/") for path in pages))
            else ()
        )
        return SelectedTargets(
            generation,
            tuple(source_ids),
            tuple(page_paths) if page_paths is not None else None,
            units,
            {path: tuple(values) for path, values in basis.items()},
            old_units,
            set(problems),
            pending,
            reasons,
            pages,
            blocked,
        )
