"""Accept one actual embedded object, its queue intent and cumulative budget atomically."""

import hashlib
from pathlib import PurePosixPath
from uuid import uuid4

from pydantic import Field

from ultrakb.pending.budget import BudgetWait
from ultrakb.pending.policies import CURRENT_POLICY, discovery_hosts
from ultrakb.pending.records import ExecutionBudget, ExecutionGroup
from ultrakb.source_records import Digest, Record, RecordId, ViewId

from .blobs import put_blob, read_bytes, retain_blob
from .catalog_store import CatalogConflict, CatalogStore
from .coordination import storage_stage
from .pending_store import PendingStore, require_running_work


class DiscoveryInput(Record):
    source_revision_id: RecordId
    policy: str = CURRENT_POLICY
    view_id: ViewId
    depth: int = Field(default=0, ge=0, le=100)
    ancestry: tuple[Digest, ...] = ()
    processing_key: str = ""


def object_identity(value, object_key, payload):
    parts = [
        value.source_revision_id,
        object_key,
        value.policy,
        hashlib.sha256(payload).hexdigest(),
    ]
    if value.processing_key:
        parts.append(value.processing_key)
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


class DiscoveryStore:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def schedule(
        self,
        revision,
        *,
        root_import_id,
        view_id,
        depth=0,
        ancestry=(),
        policy=CURRENT_POLICY,
        processing_key="",
        budget=None,
        budget_origin="default",
    ):
        main = next(file for file in revision.files if file.kind == "main")
        if PurePosixPath(main.path).suffix.lower().lstrip(".") not in discovery_hosts(policy):
            return None
        value = DiscoveryInput(
            source_revision_id=revision.revision_id,
            view_id=view_id,
            depth=depth,
            ancestry=ancestry,
            policy=policy,
            processing_key=processing_key,
        )
        encoded = value.model_dump_json().encode()
        budget = ExecutionBudget.model_validate(budget or {}).model_dump_json()
        if budget_origin not in {"default", "kb", "execution_group"}:
            raise ValueError("Unknown discovery budget origin")
        if len(encoded) > 65536:
            raise ValueError("Discovery metadata exceeds the storage item limit")
        with storage_stage(self.root, self.identity) as db:
            row = db.connection.execute(
                "SELECT intent_id FROM ukb_discovery_runs WHERE source_revision_id = ? "
                "AND policy = ? AND processing_key = ?",
                (revision.revision_id, policy, processing_key),
            ).fetchone()
            if row:
                return PendingStore._job(db.connection, row[0])
            if CatalogStore._revision(db, revision.revision_id) != revision:
                raise ValueError("Discovery requires this database's exact managed original")
            blob, intent_id = put_blob(db, encoded), uuid4().hex
            with db.transaction() as connection:
                row = connection.execute(
                    "SELECT intent_id FROM ukb_discovery_runs WHERE source_revision_id = ? "
                    "AND policy = ? AND processing_key = ?",
                    (revision.revision_id, policy, processing_key),
                ).fetchone()
                if row:
                    return PendingStore._job(connection, row[0])
                connection.execute(
                    "INSERT INTO ukb_import_groups VALUES (?, ?, ?, 0) ON CONFLICT DO NOTHING",
                    (root_import_id, self.identity.generation, budget),
                )
                group = connection.execute(
                    "SELECT generation, cancelled FROM ukb_import_groups WHERE root_import_id = ?",
                    (root_import_id,),
                ).fetchone()
                if group != (self.identity.generation, 0):
                    raise CatalogConflict("Discovery group was cancelled or replaced")
                connection.execute(
                    "INSERT INTO ukb_discovery_groups(root_import_id, budget_origin) VALUES (?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (root_import_id, budget_origin),
                )
                connection.execute(
                    "INSERT INTO ukb_import_intents(intent_id, root_import_id, source_revision_id, "
                    "work_key, kind, status) VALUES (?, ?, ?, ?, 'discovery', 'pending')",
                    (
                        intent_id,
                        root_import_id,
                        revision.revision_id,
                        f"discovery:{revision.revision_id}:{policy}:{processing_key}",
                    ),
                )
                connection.execute(
                    "INSERT INTO ukb_discovery_runs VALUES (?, ?, ?, ?, ?, 0)",
                    (intent_id, revision.revision_id, policy, processing_key, blob.digest),
                )
                retain_blob(connection, "discovery", intent_id, "input", blob)
                return PendingStore._job(connection, intent_id)

    def input(self, intent_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT metadata_digest, cursor FROM ukb_discovery_runs WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(intent_id)
            value = DiscoveryInput.model_validate_json(read_bytes(db, row[0]))
            work = PendingStore._job(db.connection, intent_id)
            if value.source_revision_id != work.source_revision_id or work.kind != "discovery":
                raise ValueError("Discovery metadata differs from its retained intent")
            return value, row[1]

    def import_root(self, source_revision_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT root_import_id FROM (SELECT i.root_import_id, i.sequence "
                "FROM ukb_import_intents i JOIN ukb_discovery_runs d "
                "ON d.intent_id = i.intent_id WHERE d.source_revision_id = ? "
                "UNION ALL SELECT i.root_import_id, i.sequence FROM ukb_pending_source_bindings b "
                "JOIN ukb_import_intents i ON i.intent_id = b.intent_id "
                "WHERE b.source_revision_id = ?) x WHERE EXISTS "
                "(SELECT 1 FROM ukb_import_groups g WHERE g.root_import_id = x.root_import_id "
                "AND g.cancelled = 0) ORDER BY sequence DESC LIMIT 1",
                (source_revision_id, source_revision_id),
            ).fetchone()
            return row[0] if row else None

    def counts(self, root_import_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            rows = db.connection.execute(
                "SELECT kind, count(*) FROM ukb_import_intents WHERE root_import_id = ? "
                "AND status NOT IN ('completed', 'stale', 'cancelled', 'not_imported') "
                "GROUP BY kind",
                (root_import_id,),
            ).fetchall()
        counts = dict(rows)
        return {
            "discovery_pending": counts.get("discovery", 0),
            "imports_pending": counts.get("import", 0),
        }

    def group(self, root_import_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT g.generation, g.budget_json, g.cancelled, d.sources, d.object_bytes, "
                "d.decompressed_bytes, d.discovery_seconds, d.budget_origin "
                "FROM ukb_import_groups g LEFT JOIN ukb_discovery_groups d "
                "ON d.root_import_id = g.root_import_id WHERE g.root_import_id = ?",
                (root_import_id,),
            ).fetchone()
            if row is None:
                raise KeyError(root_import_id)
            return ExecutionGroup(
                root_import_id=root_import_id,
                kb_generation=row[0],
                budget=ExecutionBudget.model_validate_json(row[1]),
                cancelled=bool(row[2]),
                sources=row[3] or 1,
                object_bytes=row[4] or 0,
                decompressed_bytes=row[5] or 0,
                discovery_seconds=row[6] or 0.0,
                budget_origin=row[7] or "default",
            )

    def record_meter(self, work, meter):
        updated = meter.updated_group()
        elapsed = updated.discovery_seconds - meter.group.discovery_seconds
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            require_running_work(connection, self.identity, work, allow_cancelled=True)
            connection.execute(
                "UPDATE ukb_discovery_groups SET decompressed_bytes = decompressed_bytes + ?, "
                "discovery_seconds = discovery_seconds + ? WHERE root_import_id = ?",
                (meter.decompressed, elapsed, work.root_import_id),
            )

    def checkpoint(self, intent_id, ordinal):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return db.connection.execute(
                "SELECT checkpoint_id, object_key FROM ukb_discovery_objects "
                "WHERE discovery_intent_id = ? AND ordinal = ?",
                (intent_id, ordinal),
            ).fetchone()

    def accept(
        self,
        work,
        value,
        ordinal,
        *,
        object_key,
        payload=None,
        filename=None,
        outcome,
        diagnostic=None,
    ):
        if len(object_key.encode()) + len((diagnostic or "").encode()) > 63000:
            raise ValueError("Embedded object metadata exceeds the storage item limit")
        if (
            (payload is None) != (filename is None)
            or filename
            and (PurePosixPath(filename).name != filename or "\\" in filename or ":" in filename)
        ):
            raise ValueError("Recovered documents require a contained filename and frozen bytes")
        checkpoint_id = object_identity(value, object_key, payload or b"")
        with storage_stage(self.root, self.identity) as db:
            blob = put_blob(db, payload) if payload is not None else None
            budget_json = db.connection.execute(
                "SELECT budget_json FROM ukb_import_groups WHERE root_import_id = ?",
                (work.root_import_id,),
            ).fetchone()[0]
            budget = ExecutionBudget.model_validate_json(budget_json)
            with db.transaction() as connection:
                require_running_work(connection, self.identity, work)
                existing = connection.execute(
                    "SELECT checkpoint_id FROM ukb_discovery_objects "
                    "WHERE discovery_intent_id = ? AND ordinal = ?",
                    (work.intent_id, ordinal),
                ).fetchone()
                if existing:
                    if existing != (checkpoint_id,):
                        raise ValueError("An accepted embedded object cannot change identity")
                    return checkpoint_id
                cursor = connection.execute(
                    "SELECT cursor FROM ukb_discovery_runs WHERE intent_id = ?",
                    (work.intent_id,),
                ).fetchone()
                if cursor != (ordinal,):
                    raise CatalogConflict("Discovery cursor changed before accepting this object")
                if blob:
                    row = connection.execute(
                        "SELECT g.budget_json, d.sources, d.object_bytes, d.decompressed_bytes, "
                        "d.discovery_seconds FROM ukb_import_groups g "
                        "JOIN ukb_discovery_groups d ON d.root_import_id = g.root_import_id "
                        "WHERE g.root_import_id = ?",
                        (work.root_import_id,),
                    ).fetchone()
                    if row[0] != budget_json:
                        raise CatalogConflict("The discovery budget changed before acceptance")
                    if row[3] > budget.max_decompressed_bytes:
                        raise BudgetWait("Decompression budget exhausted")
                    if row[4] >= budget.max_discovery_seconds:
                        raise BudgetWait("Discovery time budget exhausted")
                    if value.depth + 1 > budget.max_depth:
                        raise BudgetWait("Discovery depth budget exhausted")
                    if row[1] >= budget.max_sources:
                        raise BudgetWait("Source count budget exhausted")
                    if (
                        blob.byte_count > budget.max_object_bytes
                        or row[2] + blob.byte_count > budget.max_total_bytes
                    ):
                        raise BudgetWait("Object byte budget exhausted")
                    connection.execute(
                        "INSERT INTO ukb_import_intents(intent_id, root_import_id, "
                        "source_revision_id, "
                        "work_key, kind, status) VALUES (?, ?, ?, ?, 'import', 'pending')",
                        (
                            checkpoint_id,
                            work.root_import_id,
                            work.source_revision_id,
                            "recovered:" + checkpoint_id,
                        ),
                    )
                connection.execute(
                    "INSERT INTO ukb_discovery_objects(checkpoint_id, discovery_intent_id, "
                    "ordinal, "
                    "object_key, payload_digest, import_intent_id, filename, outcome, diagnostic) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        work.intent_id,
                        ordinal,
                        object_key,
                        blob.digest if blob else None,
                        checkpoint_id if blob else None,
                        filename,
                        outcome,
                        diagnostic,
                    ),
                )
                if blob:
                    retain_blob(connection, "discovery-object", checkpoint_id, "payload", blob)
                    connection.execute(
                        "UPDATE ukb_discovery_groups SET sources = sources + 1, "
                        "object_bytes = object_bytes + ? WHERE root_import_id = ?",
                        (blob.byte_count, work.root_import_id),
                    )
                connection.execute(
                    "UPDATE ukb_discovery_runs SET cursor = cursor + 1 WHERE intent_id = ?",
                    (work.intent_id,),
                )
        return checkpoint_id

    def recovered(self, intent_id):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT discovery_intent_id, object_key, payload_digest, filename "
                "FROM ukb_discovery_objects WHERE import_intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                return None
            value, _ = self.input(row[0])
            return value, row[1], row[3], read_bytes(db, row[2])

    def bind_source(self, work, revision, execution):
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            require_running_work(connection, self.identity, work)
            from .execution_store import require_active_execution

            require_active_execution(connection, self.identity, execution.execution_id)
            connection.execute(
                "INSERT INTO ukb_pending_source_bindings VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (work.intent_id, revision.revision_id, execution.execution_id),
            )
            if connection.execute(
                "SELECT source_revision_id FROM ukb_pending_source_bindings WHERE intent_id = ?",
                (work.intent_id,),
            ).fetchone() != (revision.revision_id,):
                raise CatalogConflict("Recovered import is already bound to another original")

    def update_budget(self, root_import_id, patch):
        before = self.group(root_import_id)
        if before.cancelled:
            raise CatalogConflict("A cancelled group cannot resume")
        budget = ExecutionBudget.model_validate({**before.budget.model_dump(), **patch})
        encoded = budget.model_dump_json()
        previous_budget = before.budget.model_dump_json()
        with storage_stage(self.root, self.identity) as db, db.transaction() as connection:
            changed = connection.execute(
                "UPDATE ukb_import_groups SET budget_json = ? WHERE root_import_id = ? "
                "AND generation = ? AND cancelled = 0 AND budget_json = ?",
                (
                    encoded,
                    root_import_id,
                    self.identity.generation,
                    previous_budget,
                ),
            ).rowcount
            if changed != 1:
                raise CatalogConflict("The discovery group budget changed")
            connection.execute(
                "UPDATE ukb_discovery_groups SET budget_origin = 'execution_group' "
                "WHERE root_import_id = ?",
                (root_import_id,),
            )
        if budget == before.budget or any(
            getattr(budget, field) > getattr(before.budget, field)
            for field in type(budget).model_fields
        ):
            pending = PendingStore(self.root, self.identity)
            while True:
                with storage_stage(self.root, self.identity, read_only=True) as db:
                    jobs = db.connection.execute(
                        "SELECT intent_id FROM ukb_import_intents WHERE root_import_id = ? "
                        "AND status = 'budget_wait' ORDER BY sequence LIMIT 63",
                        (root_import_id,),
                    ).fetchall()
                if not jobs:
                    break
                for [intent_id] in jobs:
                    pending.retry(intent_id)
        return self.group(root_import_id).model_dump(mode="json")
