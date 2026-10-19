"""Release obsolete recovery roots explicitly, before ordinary Blob collection."""

import hashlib
import json
from dataclasses import dataclass

from .coordination import storage_stage, try_reclamation
from .maintenance import _limits
from .receipt_store import pending_reclamation_barrier

_UNBOUND_CHECKPOINT = (
    "NOT EXISTS (SELECT 1 FROM ukb_session_heads h WHERE h.checkpoint_id = c.checkpoint_id) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_session_projections p "
    "WHERE p.checkpoint_id = c.checkpoint_id) "
    "AND EXISTS (SELECT 1 FROM ukb_blob_refs r WHERE r.owner_kind = 'conversation-checkpoint' "
    "AND r.owner_id = c.checkpoint_id AND r.slot = 'state' AND r.digest = c.state_digest) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_blob_refs r WHERE r.digest = c.state_digest "
    "AND NOT (r.owner_kind = 'conversation-checkpoint' "
    "AND r.owner_id = c.checkpoint_id AND r.slot = 'state'))"
)
_RETIRABLE_PLAN = (
    "NOT EXISTS (SELECT 1 FROM ukb_projection_state w WHERE w.active_plan_id = p.plan_id) "
    "AND (p.state = 'retiring' OR (p.state IN ('completed', 'superseded') "
    "AND EXISTS (SELECT 1 FROM ukb_projection_state w JOIN ukb_sync_plans current "
    "ON current.plan_id = w.active_plan_id WHERE w.view_id = p.view_id "
    "AND w.paused = 0 AND current.state = 'completed'))) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_blob_refs r WHERE r.digest = p.manifest_digest "
    "AND NOT (r.owner_kind = 'sync-plan' AND r.owner_id = p.plan_id AND r.slot = 'manifest'))"
)


@dataclass(frozen=True)
class RootRetirementPreview:
    candidate_checkpoints: int
    protected_checkpoints: int
    candidate_reference_bytes: int
    has_more: bool
    blocked_reason: str | None = None
    candidate_plans: int = 0
    protected_plans: int = 0
    candidate_sync_items: int = 0


@dataclass(frozen=True)
class RootRetirementResult:
    status: str
    retired_checkpoints: int = 0
    released_references: int = 0
    released_reference_bytes: int = 0
    has_more: bool = False
    blocked_reason: str | None = None
    retired_plans: int = 0
    retired_sync_items: int = 0


@dataclass(frozen=True)
class _PlanBatch:
    plan: tuple
    items: tuple
    complete: bool


def _checkpoints(db, max_items, max_bytes):
    rows = db.connection.execute(
        "SELECT c.checkpoint_id, c.state_digest, b.byte_count "
        "FROM ukb_conversation_checkpoints c JOIN ukb_blobs b ON b.digest = c.state_digest "
        f"WHERE {_UNBOUND_CHECKPOINT} ORDER BY c.checkpoint_id LIMIT ?",
        (max_items + 1,),
    ).fetchall()
    selected, encoded = [], 0
    for row in rows:
        # Bound SQL parameter bytes, independently of the immutable state size.
        cost = len(json.dumps(row).encode()) + 256
        if len(selected) >= max_items or encoded + cost > max_bytes:
            break
        selected.append(row)
        encoded += cost
    return selected, len(selected) < len(rows), encoded


def _plans(db, max_items, max_bytes):
    plans = db.connection.execute(
        "SELECT p.plan_id, p.manifest_digest, b.byte_count "
        "FROM ukb_sync_plans p JOIN ukb_blobs b ON b.digest = p.manifest_digest "
        f"WHERE {_RETIRABLE_PLAN} ORDER BY p.plan_id LIMIT ?",
        (max_items + 1,),
    ).fetchall()
    work, spent = [], 0
    for plan in plans:
        plan_cost = len(json.dumps(plan).encode()) + 512
        if max_items == 0 or spent + plan_cost > max_bytes:
            break
        rows = db.connection.execute(
            "SELECT path, expected_digest, expected_bytes, target_digest, target_bytes "
            "FROM ukb_sync_items WHERE plan_id = ? ORDER BY path LIMIT ?",
            (plan[0], max_items + 1),
        ).fetchall()
        selected, cost = [], plan_cost
        for row in rows:
            slot = hashlib.sha256(row[0].encode()).hexdigest()
            item_cost = len(json.dumps((row, slot)).encode()) + 512
            if len(selected) == max_items or spent + cost + item_cost > max_bytes:
                break
            selected.append((*row, slot))
            cost += item_cost
        if rows and not selected:
            break
        work.append(_PlanBatch(plan, tuple(selected), len(selected) == len(rows)))
        spent += cost
        max_items -= max(1, len(selected))
        if len(selected) < len(rows):
            return work, True
    return work, len(work) < len(plans)


def _retire_plan(db, plan, items):
    plan_id, digest, manifest_size = plan
    with db.transaction() as connection:
        accepted = connection.execute(
            f"UPDATE ukb_sync_plans AS p SET state = 'retiring' WHERE {_RETIRABLE_PLAN} "
            "AND p.plan_id = ?",
            (plan_id,),
        ).rowcount
    if not accepted:
        return 0, 0, 0, 0
    references = byte_count = retired_items = 0
    for path, previous, previous_size, target, target_size, slot in items:
        with db.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM ukb_sync_items WHERE plan_id = ? AND path = ?", (plan_id, path)
            ).rowcount
            if not changed:
                continue
            retired_items += 1
            for name, value, size in (
                ("expected:", previous, previous_size),
                ("target:", target, target_size),
            ):
                if value is not None:
                    released = connection.execute(
                        "DELETE FROM ukb_blob_refs WHERE owner_kind = 'sync-plan' "
                        "AND owner_id = ? AND slot = ? AND digest = ?",
                        (plan_id, name + slot, value),
                    ).rowcount
                    references += released
                    byte_count += size if released else 0
    with db.transaction() as connection:
        retired = connection.execute(
            "UPDATE ukb_sync_plans SET state = 'retired', manifest_digest = NULL, "
            "remaining_count = 0, conflict_count = 0 WHERE plan_id = ? AND state = 'retiring' "
            "AND NOT EXISTS (SELECT 1 FROM ukb_sync_items WHERE plan_id = ?)",
            (plan_id, plan_id),
        ).rowcount
        if retired:
            released = connection.execute(
                "DELETE FROM ukb_blob_refs WHERE owner_kind = 'sync-plan' "
                "AND owner_id = ? AND slot = 'manifest' AND digest = ?",
                (plan_id, digest),
            ).rowcount
            references += released
            byte_count += manifest_size if released else 0
    return retired, retired_items, references, byte_count


class RootRetirement:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def preview(self, *, max_items=128, max_bytes=256 * 1024):
        _limits(max_items, max_bytes)
        with storage_stage(self.root, self.identity, read_only=True) as db:
            reason = pending_reclamation_barrier(db)
            protected = db.connection.execute(
                "SELECT count(*) FROM ukb_conversation_checkpoints c "
                + ("" if reason else f"WHERE NOT ({_UNBOUND_CHECKPOINT})")
            ).fetchone()[0]
            rows, more, spent = ([], False, 0) if reason else _checkpoints(db, max_items, max_bytes)
            plans, plans_more = (
                ([], False) if reason else _plans(db, max_items - len(rows), max_bytes - spent)
            )
            protected_plans = db.connection.execute(
                "SELECT count(*) FROM ukb_sync_plans p WHERE p.state != 'retired' "
                + ("" if reason else f"AND NOT ({_RETIRABLE_PLAN})")
            ).fetchone()[0]
            plan_bytes = sum(
                (batch.plan[2] if batch.complete else 0)
                + sum(item[2] + item[4] for item in batch.items)
                for batch in plans
            )
            return RootRetirementPreview(
                len(rows),
                protected,
                sum(row[2] for row in rows) + plan_bytes,
                more or plans_more,
                reason,
                len(plans),
                protected_plans,
                sum(len(batch.items) for batch in plans),
            )

    def retire(self, *, max_items=128, max_bytes=256 * 1024):
        _limits(max_items, max_bytes)
        with try_reclamation(self.root, self.identity) as db:
            if db is None:
                return RootRetirementResult("busy")
            if reason := pending_reclamation_barrier(db):
                return RootRetirementResult("blocked", blocked_reason=reason)
            rows, more, spent = _checkpoints(db, max_items, max_bytes)
            plans, plans_more = _plans(db, max_items - len(rows), max_bytes - spent)
            retired = references = byte_count = 0
            for checkpoint_id, digest, size in rows:
                with db.transaction() as connection:
                    changed = connection.execute(
                        "DELETE FROM ukb_conversation_checkpoints AS c "
                        f"WHERE c.checkpoint_id = ? AND {_UNBOUND_CHECKPOINT}",
                        (checkpoint_id,),
                    ).rowcount
                    if changed:
                        released = connection.execute(
                            "DELETE FROM ukb_blob_refs WHERE owner_kind = "
                            "'conversation-checkpoint' AND owner_id = ? AND slot = 'state' "
                            "AND digest = ?",
                            (checkpoint_id, digest),
                        ).rowcount
                        retired += 1
                        references += released
                        byte_count += size if released else 0
            # These are released references, not physically reclaimed or unique bytes.
            retired_plans = retired_items = 0
            for batch in plans:
                done, count, refs, size = _retire_plan(db, batch.plan, batch.items)
                retired_plans += done
                retired_items += count
                references += refs
                byte_count += size
            return RootRetirementResult(
                "completed",
                retired,
                references,
                byte_count,
                more or plans_more,
                retired_plans=retired_plans,
                retired_sync_items=retired_items,
            )
