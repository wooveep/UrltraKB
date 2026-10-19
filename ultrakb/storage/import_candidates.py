"""Retain exact compiled import candidates and enforce reviewed publication."""

from pydantic import BaseModel, ConfigDict, TypeAdapter

from ultrakb.compile_checkpoints import digest
from ultrakb.source_records import RecordId, RelativePath

from .blobs import put_blob, read_bytes, retain_blob
from .catalog_store import CatalogConflict
from .coordination import storage_stage
from .execution_store import require_active_execution
from .generated_baselines import protected_knowledge_pages
from .knowledge_store import KnowledgeStore, PreparedRevision, PublicationConflict
from .pending_store import PendingStore
from .publication_guards import require_import_claim


class ImportReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conflicts: tuple[RelativePath, ...]


def require_import_review(connection, revision_id, version):
    row = connection.execute(
        "SELECT c.version, c.conflict_count, EXISTS (SELECT 1 FROM ukb_discarded_import_groups d "
        "WHERE d.original_id = c.original_id) FROM ukb_import_candidates c WHERE c.proposal_id = ?",
        (revision_id,),
    ).fetchone()
    if row is not None and row[2]:
        raise PublicationConflict("This import review group was explicitly discarded")
    if row is not None and row[1] and version != row[0]:
        raise PublicationConflict("This import requires its exact reviewed candidate version")


class ImportCandidates:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def retain(self, prepared, base, execution, *, original_id=None):
        if (base.root, base.identity, base.publication_id, base.publication.view_id) != (
            self.root,
            self.identity,
            prepared.expected_publication,
            prepared.view_id,
        ):
            raise ValueError("Import review requires this exact fixed compilation baseline")
        with storage_stage(self.root, self.identity) as db:
            files = dict(
                db.connection.execute(
                    "SELECT path, digest FROM ukb_revision_files WHERE revision_id = ?",
                    (prepared.revision_id,),
                ).fetchall()
            )
            from .import_review_assets import only_image_relocations

            before = base.files()
            conflicts = {
                path
                for path in protected_knowledge_pages(before, files, base.generated_baselines())
                if not only_image_relocations(db, path, before, files)
            }
            if db.connection.execute(
                "SELECT publication_id FROM ukb_heads WHERE view_id = ?", (prepared.view_id,)
            ).fetchone() != (prepared.expected_publication,):
                conflicts.add("knowledge_baseline_changed")
            details = ImportReview(conflicts=tuple(sorted(conflicts))).model_dump_json().encode()
            if len(details) > 4 * 1024 * 1024:
                raise ValueError("Import review metadata exceeds the storage payload limit")
            blob = put_blob(db, details)
            version = digest(
                {
                    "generation": self.identity.generation,
                    "candidate": prepared.revision_id,
                    "manifest": prepared.manifest_digest,
                    "review": blob.digest,
                }
            )
            with db.transaction() as connection:
                require_active_execution(connection, self.identity, execution.execution_id)
                require_import_claim(
                    connection, self.identity, prepared.revision_id, prepared.import_attempt_id
                )
                if connection.execute(
                    "SELECT state FROM ukb_knowledge_revisions WHERE revision_id = ?",
                    (prepared.revision_id,),
                ).fetchone() != ("ready",):
                    raise ValueError("An import candidate requires its complete ready revision")
                connection.execute(
                    "INSERT INTO ukb_import_candidates VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        prepared.revision_id,
                        blob.digest,
                        execution.execution_id,
                        version,
                        len(conflicts),
                        original_id or prepared.revision_id,
                    ),
                )
                retain_blob(connection, "import-candidate", prepared.revision_id, "review", blob)
        return self.read(prepared.revision_id)

    def find(self, work, view_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT c.proposal_id FROM ukb_revision_import i "
                "JOIN ukb_import_candidates c ON c.proposal_id = i.revision_id "
                "JOIN ukb_knowledge_revisions r ON r.revision_id = i.revision_id "
                "WHERE i.attempt_id = ? AND i.intent_id = ? AND r.view_id = ? "
                "AND (r.state = 'published' OR NOT EXISTS (SELECT 1 "
                "FROM ukb_discarded_import_groups d WHERE d.original_id = c.original_id)) "
                "ORDER BY c.rowid DESC LIMIT 1",
                (work.attempt_id, work.intent_id, view_id),
            ).fetchone()
        return self.read(row[0]) if row else None

    def read(self, proposal_id):
        TypeAdapter(RecordId).validate_python(proposal_id)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            db.connection.execute("BEGIN")
            row = db.connection.execute(
                "SELECT r.revision_id, r.publication_id, r.view_id, r.expected_publication, "
                "r.manifest_digest, r.navigation_fingerprint, s.plan_id, i.attempt_id, "
                "c.detail_digest, c.version, c.conflict_count, i.source_revision_id, "
                "i.unit_revision_id, "
                "u.unit_id, u.source_id, c.original_id FROM ukb_import_candidates c "
                "JOIN ukb_knowledge_revisions r ON r.revision_id = c.proposal_id "
                "JOIN ukb_revision_sync s ON s.revision_id = r.revision_id "
                "JOIN ukb_revision_import i ON i.revision_id = r.revision_id "
                "JOIN ukb_unit_revisions v ON v.unit_revision_id = i.unit_revision_id "
                "JOIN ukb_import_units u ON u.unit_id = v.unit_id "
                "WHERE c.proposal_id = ? AND r.state IN ('ready', 'published') "
                "AND (r.state = 'published' OR NOT EXISTS (SELECT 1 "
                "FROM ukb_discarded_import_groups d WHERE d.original_id = c.original_id))",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("No retained import candidate")
            prepared = PreparedRevision(*row[:8])
            details = ImportReview.model_validate_json(read_bytes(db, row[8]))
            if len(details.conflicts) != row[10]:
                raise ValueError("The retained import review metadata changed")
            published = (
                db.connection.execute(
                    "SELECT 1 FROM ukb_publication_receipts "
                    "WHERE publication_id = ? AND revision_id = ? "
                    "AND attempt_id = ?",
                    (prepared.publication_id, proposal_id, prepared.import_attempt_id),
                ).fetchone()
                is not None
            )
            current = db.connection.execute(
                "SELECT publication_id FROM ukb_heads WHERE view_id = ?", (prepared.view_id,)
            ).fetchone() == (prepared.expected_publication,)
            try:
                require_import_claim(
                    db.connection, self.identity, proposal_id, prepared.import_attempt_id
                )
            except CatalogConflict:
                current = False
            sync_current = (
                db.connection.execute(
                    "SELECT 1 FROM ukb_sync_plans s JOIN ukb_projection_state p "
                    "ON p.view_id = s.view_id WHERE s.plan_id = ? "
                    "AND s.base_generation = p.target_generation "
                    "AND s.applied_epoch = p.applied_epoch",
                    (prepared.sync_plan_id,),
                ).fetchone()
                is not None
            )
            return {
                "proposal_id": proposal_id,
                "prepared": prepared,
                "version": row[9],
                "view_id": prepared.view_id,
                "source_revision_id": row[11],
                "unit_revision_id": row[12],
                "unit_id": row[13],
                "source_id": row[14],
                "conflicts": details.conflicts,
                "sync_ready": sync_current,
                "original_id": row[15],
                "status": "accepted"
                if published
                else "superseded"
                if not current
                else "awaiting_confirmation"
                if details.conflicts
                else "ready",
            }

    def summaries(self, view_id, *, after=None, limit=63):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Import review pages require a limit of 1..128")
        with storage_stage(self.root, self.identity, read_only=True) as db:
            upper = 2**63 - 1
            if after is not None:
                TypeAdapter(RecordId).validate_python(after)
                cursor = db.connection.execute(
                    "SELECT c.rowid FROM ukb_import_candidates c JOIN ukb_knowledge_revisions r "
                    "ON r.revision_id = c.proposal_id WHERE c.proposal_id = ? AND r.view_id = ?",
                    (after, view_id),
                ).fetchone()
                if cursor is None:
                    raise FileNotFoundError("No import review cursor in this view")
                upper = cursor[0]
            rows = db.connection.execute(
                "SELECT c.proposal_id, c.version, i.unit_revision_id, i.source_revision_id, "
                "u.unit_id, u.source_id, r.expected_publication, h.publication_id, i.attempt_id "
                "FROM ukb_import_candidates c "
                "JOIN ukb_knowledge_revisions r ON r.revision_id = c.proposal_id "
                "JOIN ukb_revision_import i ON i.revision_id = c.proposal_id "
                "JOIN ukb_unit_revisions v ON v.unit_revision_id = i.unit_revision_id "
                "JOIN ukb_import_units u ON u.unit_id = v.unit_id "
                "JOIN ukb_heads h ON h.view_id = r.view_id "
                "WHERE r.view_id = ? AND r.state = 'ready' AND c.conflict_count > 0 "
                "AND NOT EXISTS (SELECT 1 FROM ukb_discarded_import_groups d "
                "WHERE d.original_id = c.original_id) "
                "AND c.rowid < ? "
                "AND NOT EXISTS (SELECT 1 FROM ukb_import_candidates newer "
                "WHERE newer.original_id = c.original_id AND newer.rowid > c.rowid) "
                "ORDER BY c.rowid DESC LIMIT ?",
                (view_id, upper, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                current = row[6] == row[7]
                try:
                    require_import_claim(db.connection, self.identity, row[0], row[8])
                except CatalogConflict:
                    current = False
                items.append(
                    {
                        "proposal_id": row[0],
                        "version": row[1],
                        "unit_revision_id": row[2],
                        "source_revision_id": row[3],
                        "unit_id": row[4],
                        "source_id": row[5],
                        "view_id": view_id,
                        "conflicts": (),
                        "status": "awaiting_confirmation" if current else "superseded",
                    }
                )
        return tuple(items), rows[limit - 1][0] if len(rows) > limit else None

    def reprepare_sync(self, candidate, execution):
        """Rebuild only unpublished plans from exact accepted content and input identities."""
        if candidate["status"] not in {"ready", "awaiting_confirmation"}:
            raise PublicationConflict("The compilation content baseline or input changed")
        with storage_stage(self.root, self.identity, read_only=True) as db:
            latest = db.connection.execute(
                "SELECT proposal_id FROM ukb_import_candidates WHERE original_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (candidate["original_id"],),
            ).fetchone()
            claim = db.connection.execute(
                "SELECT intent_id FROM ukb_revision_import WHERE revision_id = ?",
                (candidate["proposal_id"],),
            ).fetchone()
            work = PendingStore._job(db.connection, claim[0])
            inputs = db.connection.execute(
                "SELECT unit_revision_id, index_revision_id FROM ukb_revision_inputs "
                "WHERE revision_id = ? ORDER BY unit_revision_id",
                (candidate["proposal_id"],),
            ).fetchall()
            page_inputs = {}
            for path, unit in db.connection.execute(
                "SELECT path, unit_revision_id FROM ukb_revision_page_inputs "
                "WHERE revision_id = ? ORDER BY path, unit_revision_id",
                (candidate["proposal_id"],),
            ).fetchall():
                page_inputs.setdefault(path, []).append(unit)
            generated = dict(
                db.connection.execute(
                    "SELECT path, digest FROM ukb_revision_generated WHERE revision_id = ?",
                    (candidate["proposal_id"],),
                ).fetchall()
            )
        if latest is not None and latest[0] != candidate["proposal_id"]:
            retained = self.read(latest[0])
            if retained["status"] in {"ready", "awaiting_confirmation"} and retained["sync_ready"]:
                return retained
        store = KnowledgeStore(self.root, self.identity)
        base = store.bind(candidate["prepared"].expected_publication, view_id=candidate["view_id"])
        old, new = base.files(), self.files(candidate["proposal_id"])
        changes = {
            path: self.read_file(candidate["proposal_id"], path) if path in new else None
            for path in old.keys() | new.keys()
            if old.get(path) != new.get(path)
        }
        prior_generated = base.generated_baselines()
        prepared = store._prepare_revision(
            changes,
            expected=base.publication_id,
            view_id=candidate["view_id"],
            base=base,
            import_work=work,
            unit_revisions=tuple(unit for unit, _ in inputs),
            index_revisions=tuple(index for _, index in inputs if index is not None),
            page_inputs={path: tuple(units) for path, units in page_inputs.items()},
            generated_paths=tuple(
                path
                for path, value in generated.items()
                if path in new and value != prior_generated.get(path)
            ),
            compile_outcome=store.compilation_outcome(candidate["proposal_id"]),
        )
        return self.retain(prepared, base, execution, original_id=candidate["original_id"])

    def files(self, proposal_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return dict(
                db.connection.execute(
                    "SELECT path, digest FROM ukb_revision_files "
                    "WHERE revision_id = ? ORDER BY path",
                    (proposal_id,),
                ).fetchall()
            )

    def read_file(self, proposal_id, path):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT digest FROM ukb_revision_files WHERE revision_id = ? AND path = ?",
                (proposal_id, path),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(path)
            return read_bytes(db, row[0])
