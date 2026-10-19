"""Release abandoned building plans; ready plans and accepted calculations stay roots."""

import json
from dataclasses import dataclass

ELIGIBLE = (
    "g.state IN ('building', 'retiring') "
    "AND NOT EXISTS (SELECT 1 FROM ukb_executions e "
    "WHERE e.model_budget_id = g.model_budget_id AND e.state IN ('started', 'stopping')) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_generation_files f "
    "JOIN ukb_artifact_revisions a ON a.revision_id = f.revision_id WHERE f.plan_id = g.plan_id) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_blob_refs r WHERE r.owner_kind = 'generator-plan' "
    "AND r.owner_id = g.plan_id AND NOT ((r.slot = 'details' AND r.digest = g.detail_digest) "
    "OR EXISTS (SELECT 1 FROM ukb_generation_files f WHERE f.plan_id = g.plan_id "
    "AND f.path = r.slot AND f.digest = r.digest)))"
)


@dataclass(frozen=True)
class _Batch:
    plan: tuple
    files: tuple
    complete: bool


def select(db, max_items, max_bytes):
    plans = db.connection.execute(
        "SELECT g.plan_id, g.detail_digest, b.byte_count FROM ukb_generation_plans g "
        "JOIN ukb_blobs b ON b.digest = g.detail_digest "
        f"WHERE {ELIGIBLE} ORDER BY g.plan_id LIMIT ?",
        (max_items + 1,),
    ).fetchall()
    work, spent = [], 0
    for plan in plans:
        cost = len(json.dumps(plan).encode()) + 512
        if max_items == 0 or spent + cost > max_bytes:
            break
        rows = db.connection.execute(
            "SELECT ordinal, path, digest, byte_count FROM ukb_generation_files "
            "WHERE plan_id = ? ORDER BY ordinal LIMIT ?",
            (plan[0], max_items + 1),
        ).fetchall()
        selected = []
        for row in rows:
            row_cost = len(json.dumps(row).encode()) + 512
            if len(selected) >= max_items or spent + cost + row_cost > max_bytes:
                break
            selected.append(row)
            cost += row_cost
        if rows and not selected:
            break
        work.append(_Batch(plan, tuple(selected), len(selected) == len(rows)))
        spent += cost
        max_items -= max(1, len(selected))
        if len(selected) < len(rows):
            return work, True, spent
    return work, len(work) < len(plans), spent


def protected_count(db, *, blocked=False):
    return db.connection.execute(
        "SELECT count(*) FROM ukb_generation_plans g "
        + ("" if blocked else f"WHERE NOT ({ELIGIBLE})")
    ).fetchone()[0]


def reference_bytes(batches):
    return sum(
        (batch.plan[2] if batch.complete else 0) + sum(row[3] for row in batch.files)
        for batch in batches
    )


def retire(db, batch):
    plan_id, detail_digest, detail_size = batch.plan
    with db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_generation_plans AS g SET state = 'retiring' "
            f"WHERE g.plan_id = ? AND g.state = 'building' AND {ELIGIBLE}",
            (plan_id,),
        )
        if not connection.execute(
            "SELECT 1 FROM ukb_generation_plans g WHERE g.plan_id = ? "
            f"AND g.state = 'retiring' AND {ELIGIBLE}",
            (plan_id,),
        ).fetchone():
            return 0, 0, 0, 0
    files = refs = size = 0
    for ordinal, path, digest, byte_count in batch.files:
        with db.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM ukb_generation_files WHERE plan_id = ? AND ordinal = ? "
                "AND path = ? AND digest = ?",
                (plan_id, ordinal, path, digest),
            ).rowcount
            if changed:
                released = connection.execute(
                    "DELETE FROM ukb_blob_refs WHERE owner_kind = 'generator-plan' "
                    "AND owner_id = ? AND slot = ? AND digest = ?",
                    (plan_id, path, digest),
                ).rowcount
                files += 1
                refs += released
                size += byte_count if released else 0
    with db.transaction() as connection:
        retired = connection.execute(
            "DELETE FROM ukb_generation_plans AS g WHERE g.plan_id = ? "
            f"AND g.state = 'retiring' AND {ELIGIBLE} "
            "AND NOT EXISTS (SELECT 1 FROM ukb_generation_files WHERE plan_id = ?)",
            (plan_id, plan_id),
        ).rowcount
        if retired:
            released = connection.execute(
                "DELETE FROM ukb_blob_refs WHERE owner_kind = 'generator-plan' "
                "AND owner_id = ? AND slot = 'details' AND digest = ?",
                (plan_id, detail_digest),
            ).rowcount
            refs += released
            size += detail_size if released else 0
    return retired, files, refs, size
