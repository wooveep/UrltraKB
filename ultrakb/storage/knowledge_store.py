"""Immutable revision manifests and short, version-checked publication commits.

This is a development storage boundary. Import success, ready navigation,
synchronization plans and publication receipts bind in one guarded commit.
"""

import json
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ultrakb.locks import file_write_lock
from ultrakb.storage.blobs import read_bytes
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.execution_store import require_active_execution
from ultrakb.storage.knowledge_inputs import (
    navigation_inputs,
)
from ultrakb.storage.navigation_store import (
    RecoveryRequired,
    bind_navigation,
    prepare_navigation,
    require_navigation,
)
from ultrakb.storage.pending_store import PendingWork
from ultrakb.storage.publication_guards import (
    claim_identity,
    finish_import_claim,
    import_claim,
    require_import_claim,
)
from ultrakb.storage.tree_store import TreeStore
from ultrakb.storage.wiki_paths import wiki_path as _path
from ultrakb.storage.wiki_projection import (
    activate_sync_plan,
    projection_lock,
)

MAX_BATCH_ROWS = 128
MAX_BATCH_BYTES = 256 * 1024
MAX_ITEM_BYTES = 64 * 1024


class PublicationConflict(ValueError):
    """Recompute or review against the new publication; never overwrite it."""


@dataclass(frozen=True)
class PreparedRevision:
    revision_id: str
    publication_id: str
    view_id: str
    expected_publication: str | None
    manifest_digest: str
    navigation_fingerprint: str
    sync_plan_id: str
    import_attempt_id: str | None = None


@dataclass(frozen=True)
class Publication:
    publication_id: str
    revision_id: str
    view_id: str


@dataclass(frozen=True)
class PublicationReceipt:
    publication_id: str
    revision_id: str
    generation: str
    attempt_id: str | None
    published_at: str
    execution_id: str | None


def _manifest(entries):
    return json.dumps(entries, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _batches(entries):
    batch, byte_count = [], 0
    for entry in entries:
        size = len(_manifest(entry)) + 512
        if size > MAX_ITEM_BYTES:
            raise ValueError("Revision entry exceeds the item limit")
        # Every file inserts one manifest row and one reference row.
        cost = 2 * size
        if batch and (2 * len(batch) >= MAX_BATCH_ROWS or byte_count + cost > MAX_BATCH_BYTES):
            yield batch
            batch, byte_count = [], 0
        batch.append(entry)
        byte_count += cost
    if batch:
        yield batch


class BoundKnowledge:
    def __init__(self, root: Path, identity: DatabaseIdentity, publication: Publication):
        self.root, self.identity, self.publication = root, identity, publication

    @property
    def publication_id(self):
        return self.publication.publication_id

    def read(self, path: str) -> bytes:
        _path(path)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT f.digest FROM ukb_publications p JOIN ukb_revision_files f "
                "ON f.revision_id = p.revision_id WHERE p.publication_id = ? "
                "AND p.revision_id = ? AND p.view_id = ? AND f.path = ?",
                (self.publication_id, self.publication.revision_id, self.publication.view_id, path),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(path)
            return read_bytes(db, row[0])

    def files(self) -> dict[str, str]:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return dict(
                db.connection.execute(
                    "SELECT f.path, f.digest FROM ukb_publications p JOIN ukb_revision_files f "
                    "ON f.revision_id = p.revision_id WHERE p.publication_id = ? "
                    "AND p.revision_id = ? AND p.view_id = ? ORDER BY f.path",
                    (self.publication_id, self.publication.revision_id, self.publication.view_id),
                ).fetchall()
            )

    def file_page(self, *, prefix, after=None, limit=63):
        """Read bounded manifest names, without opening the manifest or file bodies."""
        if not isinstance(prefix, str) or not prefix.endswith("/"):
            raise ValueError("Select a logical knowledge directory")
        _path(prefix[:-1])
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Knowledge page size must be between 1 and 128")
        from .wiki_paths import page_after

        lower, exclusive = page_after(prefix, after)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._require_publication(db)
            rows = db.connection.execute(
                "SELECT path FROM ukb_revision_files WHERE revision_id = ? "
                "AND path >= ? AND path < ? AND path > ? ORDER BY path LIMIT ?",
                (self.publication.revision_id, lower, prefix[:-1] + "0", exclusive, limit),
            ).fetchall()
            return tuple(row[0] for row in rows)

    def file_digest(self, path):
        _path(path)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._require_publication(db)
            row = db.connection.execute(
                "SELECT digest FROM ukb_revision_files WHERE revision_id = ? AND path = ?",
                (self.publication.revision_id, path),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(path)
            return row[0]

    def index_revisions(self) -> tuple[str, ...]:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            rows = db.connection.execute(
                "SELECT i.index_revision_id FROM ukb_publications p JOIN ukb_revision_inputs i "
                "ON i.revision_id = p.revision_id WHERE p.publication_id = ? "
                "AND p.revision_id = ? AND p.view_id = ? AND i.index_revision_id IS NOT NULL "
                "ORDER BY i.index_revision_id",
                (self.publication_id, self.publication.revision_id, self.publication.view_id),
            ).fetchall()
            return tuple(row[0] for row in rows)

    def unit_revisions(self) -> tuple[str, ...]:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._require_publication(db)
            return tuple(
                row[0]
                for row in db.connection.execute(
                    "SELECT unit_revision_id FROM ukb_revision_inputs "
                    "WHERE revision_id = ? ORDER BY unit_revision_id",
                    (self.publication.revision_id,),
                ).fetchall()
            )

    @contextmanager
    def navigation(self):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._require_publication(db)
            revision = require_navigation(db, self.publication.revision_id)
            with TreeStore(self.root, self.identity).open(revision.tree_id) as bound:
                yield bound

    def navigation_revision(self):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._require_publication(db)
            return require_navigation(db, self.publication.revision_id)

    def _require_publication(self, db):
        if (
            db.connection.execute(
                "SELECT 1 FROM ukb_publications WHERE publication_id = ? AND revision_id = ? "
                "AND view_id = ?",
                (self.publication_id, self.publication.revision_id, self.publication.view_id),
            ).fetchone()
            is None
        ):
            raise FileNotFoundError("This exact knowledge publication is unavailable")

    def page_input_revisions(self):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._require_publication(db)
            result = {}
            for path, unit in db.connection.execute(
                "SELECT path, unit_revision_id FROM ukb_revision_page_inputs "
                "WHERE revision_id = ? ORDER BY path, unit_revision_id",
                (self.publication.revision_id,),
            ).fetchall():
                result.setdefault(path, []).append(unit)
            return {path: tuple(units) for path, units in result.items()}

    def generated_baselines(self):
        from .generated_baselines import read_baselines

        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._require_publication(db)
            return {
                path: digest
                for path, digest, _ in read_baselines(db.connection, self.publication.revision_id)
            }

    def compile_workspace(self, *, required_attachments: tuple[str, ...] = ()):
        from .compile_workspace import materialize_baseline

        return materialize_baseline(self, required_attachments)

    def input_units(self, *, page: str | None = None):
        from .catalog_store import CatalogStore

        with storage_stage(self.root, self.identity, read_only=True) as db:
            self._require_publication(db)
            if page is None:
                rows = db.connection.execute(
                    "SELECT unit_revision_id FROM ukb_revision_inputs WHERE revision_id = ? "
                    "UNION SELECT unit_revision_id FROM ukb_revision_page_inputs "
                    "WHERE revision_id = ?",
                    (self.publication.revision_id, self.publication.revision_id),
                ).fetchall()
            else:
                _path(page)
                rows = db.connection.execute(
                    "SELECT unit_revision_id FROM ukb_revision_page_inputs "
                    "WHERE revision_id = ? AND path = ? ORDER BY unit_revision_id",
                    (self.publication.revision_id, page),
                ).fetchall()
            return tuple(CatalogStore._unit_revision(db, row[0]) for row in rows)

    def _source_unit(self, db, unit_revision_id):
        from .catalog_store import CatalogStore

        row = db.connection.execute(
            "SELECT 1 FROM ukb_publications p WHERE p.publication_id = ? AND p.revision_id = ? "
            "AND p.view_id = ? AND (EXISTS (SELECT 1 FROM ukb_revision_inputs i "
            "WHERE i.revision_id = p.revision_id AND i.unit_revision_id = ?) "
            "OR EXISTS (SELECT 1 FROM ukb_revision_page_inputs i "
            "WHERE i.revision_id = p.revision_id AND i.unit_revision_id = ?))",
            (
                self.publication_id,
                self.publication.revision_id,
                self.publication.view_id,
                unit_revision_id,
                unit_revision_id,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("Source input is outside this publication")
        return CatalogStore._unit_revision(db, unit_revision_id)

    def read_source(self, unit_revision_id: str, path: str) -> bytes:
        from .catalog_store import CatalogStore

        with storage_stage(self.root, self.identity, read_only=True) as db:
            unit = self._source_unit(db, unit_revision_id)
            return CatalogStore(self.root, self.identity).read_processed(unit.processing_id, path)

    def read_original(self, unit_revision_id: str) -> bytes:
        from .catalog_store import CatalogStore

        with storage_stage(self.root, self.identity, read_only=True) as db:
            unit = self._source_unit(db, unit_revision_id)
            return CatalogStore(self.root, self.identity).read_original(unit.source_revision_id)


class KnowledgeStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity

    def prepare_revision(
        self,
        files: dict[str, bytes],
        *,
        expected: str | None,
        view_id: str = "legacy",
        index_revisions: tuple[str, ...] = (),
        unit_revisions: tuple[str, ...] = (),
        import_work: PendingWork | None = None,
        page_inputs: dict[str, tuple[str, ...]] | None = None,
        generated_paths: tuple[str, ...] = (),
    ) -> PreparedRevision:
        return self._prepare_revision(
            files,
            expected=expected,
            view_id=view_id,
            index_revisions=index_revisions,
            unit_revisions=unit_revisions,
            import_work=import_work,
            page_inputs=page_inputs,
            generated_paths=generated_paths,
        )

    def prepare_changes(
        self,
        base: BoundKnowledge,
        changes: dict[str, bytes | None],
        *,
        page_inputs: dict[str, tuple[str, ...]] | None = None,
        withdrawal=None,
        generated_paths=(),
        refresh_generation=None,
    ) -> PreparedRevision:
        """Keep a fixed revision's inputs and reuse its verified immutable assets."""
        if base.root != self.root or base.identity != self.identity:
            raise ValueError("Changes require a baseline from this exact database")
        basis = base.page_input_revisions()
        if page_inputs is not None:
            if not isinstance(page_inputs, dict):
                raise ValueError("Changed page inputs must be a mapping")
            allowed = {unit.unit_revision_id for unit in base.input_units()}
            for path, units in page_inputs.items():
                if (
                    path not in changes
                    or changes[path] is None
                    or not path.endswith(".md")
                    or not isinstance(units, tuple)
                    or any(not isinstance(unit, str) or unit not in allowed for unit in units)
                ):
                    raise ValueError("Changed page inputs must belong to this exact baseline")
            basis.update(page_inputs)
        return self._prepare_revision(
            changes,
            expected=base.publication_id,
            view_id=base.publication.view_id,
            unit_revisions=base.unit_revisions(),
            index_revisions=base.index_revisions(),
            page_inputs={
                path: basis.get(path, ())
                for path in base.files()
                if path.endswith(".md") and (path not in changes or changes[path] is not None)
            }
            | (page_inputs or {}),
            base=base,
            withdrawal=withdrawal,
            generated_paths=generated_paths,
            refresh_generation=refresh_generation,
        )

    def _prepare_revision(
        self,
        files,
        *,
        expected: str | None,
        view_id: str,
        index_revisions: tuple[str, ...],
        unit_revisions: tuple[str, ...],
        import_work: PendingWork | None = None,
        compile_outcome=None,
        page_inputs: dict[str, tuple[str, ...]] | None = None,
        base: BoundKnowledge | None = None,
        withdrawal=None,
        generated_paths=(),
        refresh_generation=None,
    ) -> PreparedRevision:
        from .knowledge_preparation import prepare_revision

        return prepare_revision(
            self,
            files,
            expected=expected,
            view_id=view_id,
            unit_revisions=unit_revisions,
            index_revisions=index_revisions,
            import_work=import_work,
            compile_outcome=compile_outcome,
            page_inputs=page_inputs,
            base=base,
            withdrawal=withdrawal,
            generated_paths=generated_paths,
            refresh_generation=refresh_generation,
        )

    def compilation_outcome(self, revision_id):
        from .compile_outcomes import read_outcome

        with storage_stage(self.root, self.identity, read_only=True) as db:
            return read_outcome(db.connection, revision_id)

    def publish(
        self,
        prepared: PreparedRevision,
        *,
        edit_id: str | None = None,
        check_cancelled: Callable[[], None] = lambda: None,
        execution_id: str | None = None,
    ) -> Publication:
        def cancelled():
            check_cancelled()
            return False

        with (
            storage_stage(self.root, self.identity) as db,
            projection_lock(self.root, cancelled=cancelled),
            file_write_lock(self.root / ".ultrakb/ingest.lock", cancelled=cancelled),
            db.transaction() as connection,
        ):
            record = connection.execute(
                "SELECT publication_id, view_id, expected_publication, manifest_digest, "
                "navigation_fingerprint, state "
                "FROM ukb_knowledge_revisions WHERE revision_id = ?",
                (prepared.revision_id,),
            ).fetchone()
            if (
                record is None
                or record[:-1]
                != (
                    prepared.publication_id,
                    prepared.view_id,
                    prepared.expected_publication,
                    prepared.manifest_digest,
                    prepared.navigation_fingerprint,
                )
                or record[-1] not in {"ready", "published"}
            ):
                raise ValueError("Only the exact validated revision can be published")
            plan = connection.execute(
                "SELECT plan_id FROM ukb_revision_sync WHERE revision_id = ?",
                (prepared.revision_id,),
            ).fetchone()
            if plan != (prepared.sync_plan_id,):
                raise ValueError("Publication requires its exact synchronization plan")
            require_navigation(db, prepared.revision_id)
            if record[-1] == "published":
                receipt = connection.execute(
                    "SELECT attempt_id FROM ukb_publication_receipts WHERE publication_id = ? "
                    "AND revision_id = ?",
                    (prepared.publication_id, prepared.revision_id),
                ).fetchone()
                if receipt != (prepared.import_attempt_id,):
                    raise RecoveryRequired("Publication requires its exact committed receipt")
                return Publication(prepared.publication_id, prepared.revision_id, prepared.view_id)
            head = connection.execute(
                "SELECT publication_id FROM ukb_heads WHERE view_id = ?", (prepared.view_id,)
            ).fetchone()
            if (head[0] if head else None) != prepared.expected_publication:
                raise PublicationConflict("Knowledge changed; recompute or review the candidate")
            require_import_claim(
                connection, self.identity, prepared.revision_id, prepared.import_attempt_id
            )
            if execution_id is not None:
                require_active_execution(connection, self.identity, execution_id)
            # The callback only checks an in-memory stop/deadline signal. Do not
            # accept late preparation after cancellation, or run I/O in this commit.
            check_cancelled()
            from .generated_baselines import require_refresh_catalog
            from .source_withdrawal import finish_withdrawal

            require_refresh_catalog(connection, prepared.revision_id)
            finish_withdrawal(connection, prepared.revision_id)
            activate_sync_plan(db, prepared.revision_id, prepared.sync_plan_id)
            connection.execute(
                "INSERT INTO ukb_publications VALUES (?, ?, ?, ?)",
                (
                    prepared.publication_id,
                    prepared.revision_id,
                    prepared.view_id,
                    self.identity.generation,
                ),
            )
            connection.execute(
                "INSERT INTO ukb_heads VALUES (?, ?) ON CONFLICT(view_id) "
                "DO UPDATE SET publication_id = excluded.publication_id",
                (prepared.view_id, prepared.publication_id),
            )
            connection.execute(
                "UPDATE ukb_knowledge_revisions SET state = 'published' WHERE revision_id = ?",
                (prepared.revision_id,),
            )
            connection.execute(
                "INSERT INTO ukb_publication_receipts VALUES (?, ?, ?, ?, "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
                (
                    prepared.publication_id,
                    prepared.revision_id,
                    self.identity.generation,
                    prepared.import_attempt_id,
                    execution_id,
                ),
            )
            finish_import_claim(connection, prepared.revision_id, prepared.publication_id)
            if edit_id is not None:
                from .wiki_edit_store import finish_edit

                finish_edit(
                    connection, prepared.view_id, edit_id, prepared.publication_id, "accepted"
                )
        return Publication(prepared.publication_id, prepared.revision_id, prepared.view_id)

    def committed_publication(self, prepared: PreparedRevision) -> Publication | None:
        """Confirm an exact durable result after an acknowledgement was lost."""
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT p.publication_id, p.revision_id, p.view_id FROM ukb_publications p "
                "JOIN ukb_knowledge_revisions r ON r.revision_id = p.revision_id "
                "AND r.publication_id = p.publication_id AND r.view_id = p.view_id "
                "JOIN ukb_publication_receipts c ON c.publication_id = p.publication_id "
                "AND c.revision_id = p.revision_id AND c.generation = p.generation "
                "JOIN ukb_revision_sync s ON s.revision_id = r.revision_id "
                "WHERE r.revision_id = ? AND r.publication_id = ? AND r.view_id = ? "
                "AND r.expected_publication IS ? AND r.manifest_digest = ? "
                "AND r.navigation_fingerprint = ? AND r.state = 'published' "
                "AND s.plan_id = ? AND c.attempt_id IS ?",
                (
                    prepared.revision_id,
                    prepared.publication_id,
                    prepared.view_id,
                    prepared.expected_publication,
                    prepared.manifest_digest,
                    prepared.navigation_fingerprint,
                    prepared.sync_plan_id,
                    prepared.import_attempt_id,
                ),
            ).fetchone()
            if row is None:
                return None
            require_navigation(db, prepared.revision_id)
            return Publication(*row)

    def committed_import(self, work: PendingWork, *, view_id="legacy") -> PreparedRevision | None:
        """Recover the same committed outcome without preparing another candidate."""
        claimed = claim_identity(self.identity, work)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT r.revision_id, r.publication_id, r.view_id, r.expected_publication, "
                "r.manifest_digest, r.navigation_fingerprint, s.plan_id, c.attempt_id "
                "FROM ukb_publication_receipts c JOIN ukb_knowledge_revisions r "
                "ON r.revision_id = c.revision_id AND r.publication_id = c.publication_id "
                "JOIN ukb_revision_sync s ON s.revision_id = r.revision_id "
                "WHERE c.attempt_id = ? AND c.generation = ? AND r.view_id = ? "
                "AND r.state = 'published'",
                (work.attempt_id, self.identity.generation, view_id),
            ).fetchone()
            if row is None or import_claim(db.connection, row[0]) != claimed:
                return None
            return PreparedRevision(*row)

    def receipt(self, publication_id: str) -> PublicationReceipt:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT publication_id, revision_id, generation, attempt_id, published_at, "
                "execution_id "
                "FROM ukb_publication_receipts WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("This publication has no committed receipt")
            return PublicationReceipt(*row)

    def bind(self, publication_id: str | None = None, *, view_id: str = "legacy") -> BoundKnowledge:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if publication_id is None:
                head = db.connection.execute(
                    "SELECT publication_id FROM ukb_heads WHERE view_id = ?", (view_id,)
                ).fetchone()
                if not head:
                    raise FileNotFoundError("No complete public knowledge is available")
                publication_id = head[0]
            row = db.connection.execute(
                "SELECT revision_id FROM ukb_publications WHERE publication_id = ? AND view_id = ?",
                (publication_id, view_id),
            ).fetchone()
            if not row:
                raise FileNotFoundError("This knowledge publication is unavailable")
            require_navigation(db, row[0])
            return BoundKnowledge(
                self.root, self.identity, Publication(publication_id, row[0], view_id)
            )

    def repair_navigation(self, publication_id: str, *, view_id: str = "legacy"):
        """Rebuild the same deterministic input only through this explicit use case."""
        with storage_stage(self.root, self.identity) as db:
            record = db.connection.execute(
                "SELECT r.revision_id, r.navigation_fingerprint FROM ukb_publications p "
                "JOIN ukb_knowledge_revisions r ON r.revision_id = p.revision_id "
                "WHERE p.publication_id = ? AND p.view_id = ?",
                (publication_id, view_id),
            ).fetchone()
            if not record:
                raise FileNotFoundError("This knowledge publication is unavailable")
            try:
                return require_navigation(db, record[0])
            except RecoveryRequired:
                pass
            files = db.connection.execute(
                "SELECT path, digest, byte_count FROM ukb_revision_files "
                "WHERE revision_id = ? ORDER BY path",
                (record[0],),
            ).fetchall()
            inputs = db.connection.execute(
                "SELECT unit_revision_id, index_revision_id FROM ukb_revision_inputs "
                "WHERE revision_id = ? ORDER BY unit_revision_id",
                (record[0],),
            ).fetchall()
            tree = prepare_navigation(
                self.root,
                self.identity,
                record[0],
                publication_id,
                files,
                navigation_inputs(
                    inputs,
                    db.connection.execute(
                        "SELECT path, unit_revision_id FROM ukb_revision_page_inputs "
                        "WHERE revision_id = ? ORDER BY path, unit_revision_id",
                        (record[0],),
                    ).fetchall(),
                ),
                primary_units=tuple(unit for unit, _ in inputs),
            )
            if tree.input_fingerprint != record[1]:
                raise RecoveryRequired("Knowledge navigation policy or committed inputs changed")
            with db.transaction() as connection:
                bind_navigation(connection, record[0], tree)
            return tree
