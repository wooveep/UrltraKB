"""Bounded pending metadata lists; delivered compiler details load only on selection."""

import json
from dataclasses import asdict

from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import database_path
from ultrakb.storage.discovery_store import DiscoveryStore
from ultrakb.storage.pending_results import PendingResults
from ultrakb.storage.pending_store import PendingStore
from ultrakb.storage.usage_store import UsageStore


def checkpoint_page(scope, intent_id, *, after=0, limit=63):
    if (
        type(after) is not int
        or not 0 <= after < 2**63
        or type(limit) is not int
        or not 1 <= limit <= 128
    ):
        raise ValueError("Checkpoint details require a bounded cursor page")
    with storage_stage(scope.kb_dir, scope.knowledge.identity, read_only=True) as db:
        rows = db.connection.execute(
            "SELECT o.sequence, o.checkpoint_id, o.object_key, r.policy, o.payload_digest, "
            "o.import_intent_id, o.outcome, o.diagnostic FROM ukb_discovery_objects o "
            "JOIN ukb_discovery_runs r ON r.intent_id = o.discovery_intent_id "
            "WHERE o.discovery_intent_id = ? AND o.sequence > ? ORDER BY o.sequence LIMIT ?",
            (intent_id, after, limit + 1),
        ).fetchall()
    page = rows[:limit]
    return {
        "checkpoints": [
            dict(
                zip(
                    (
                        "sequence",
                        "checkpoint_id",
                        "object_key",
                        "policy",
                        "digest",
                        "import_intent_id",
                        "outcome",
                        "diagnostic",
                    ),
                    row,
                    strict=True,
                )
            )
            | {"discovery_intent_id": intent_id}
            for row in page
        ],
        "next_checkpoint_after": page[-1][0] if len(rows) > limit else None,
    }


def stored_job(scope, intent_id, *, details=False, checkpoint_after=0, limit=63):
    root, identity = scope.kb_dir, scope.knowledge.identity
    pending = PendingStore(root, identity)
    work = pending.job(intent_id)
    with storage_stage(root, identity, read_only=True) as db:
        row = db.connection.execute(
            "SELECT d.cursor, o.filename, coalesce(b.source_revision_id, i.source_revision_id), "
            "r.summary_json, a.message, s.source_id, s.label, b.source_revision_id, "
            "(SELECT e.state FROM ukb_executions e WHERE e.execution_id = a.execution_id) "
            "FROM ukb_import_intents i LEFT JOIN ukb_discovery_runs d ON d.intent_id = i.intent_id "
            "LEFT JOIN ukb_discovery_objects o ON o.import_intent_id = i.intent_id "
            "LEFT JOIN ukb_pending_source_bindings b ON b.intent_id = i.intent_id "
            "LEFT JOIN ukb_pending_import_results r ON r.attempt_id = i.attempt_id "
            "LEFT JOIN ukb_pending_attempts a ON a.attempt_id = i.attempt_id "
            "JOIN ukb_source_revisions sr ON sr.revision_id = "
            "coalesce(b.source_revision_id, i.source_revision_id) "
            "JOIN ukb_sources s ON s.source_id = sr.source_id WHERE i.intent_id = ?",
            (intent_id,),
        ).fetchone()
    value = {
        **asdict(work),
        "status": "interrupted"
        if work.status == "started" and row[8] is not None and row[8] not in {"started", "stopping"}
        else work.status,
        "id": intent_id,
        "cursor": row[0],
        "filename": row[1] or row[6],
        "source_id": row[5] if row[7] is not None or row[1] is None else None,
        "source_revision_id": row[2] if row[7] is not None or row[1] is None else None,
        "message": row[4],
        "result": None,
        "has_result": row[3] is not None,
        "quality_known": False,
        "quality": [],
        "unfinished": [],
        "model_usage": None,
        **(json.loads(row[3]) if row[3] else {}),
    }
    if details and work.attempt_id is not None:
        retained = PendingResults(root, identity).read(work.attempt_id)
        if retained:
            result, _ = retained
            value.update(
                result=asdict(result),
                model_usage=result.model_usage,
                quality=list(result.quality),
                unfinished=list(result.unfinished),
                message=result.message,
            )
    if details and work.kind == "discovery":
        value.update(checkpoint_page(scope, intent_id, after=checkpoint_after, limit=limit))
    return value


def stored_pending_status(scope, *, after=0, limit=63):
    if (
        type(after) is not int
        or not 0 <= after < 2**63
        or type(limit) is not int
        or not 1 <= limit <= 128
    ):
        raise ValueError("Pending lists require a nonnegative cursor and a limit of 1..128")
    root, identity = scope.kb_dir, scope.knowledge.identity
    with storage_stage(root, identity, read_only=True) as db:
        rows = db.connection.execute(
            "SELECT intent_id, sequence FROM ukb_import_intents WHERE sequence > ? "
            "AND (unit_revision_id IS NULL OR status != 'completed') ORDER BY sequence LIMIT ?",
            (after, limit + 1),
        ).fetchall()
        counts = db.connection.execute(
            "SELECT i.kind, i.status, count(*) FROM ukb_import_intents i "
            "JOIN ukb_import_groups g ON g.root_import_id = i.root_import_id "
            "WHERE g.generation = ? AND g.cancelled = 0 GROUP BY i.kind, i.status",
            (identity.generation,),
        ).fetchall()
        checkpoints = db.connection.execute(
            "SELECT o.checkpoint_id, o.discovery_intent_id, o.object_key, r.policy, "
            "o.payload_digest, o.import_intent_id, o.outcome, o.diagnostic "
            "FROM ukb_discovery_objects o JOIN ukb_discovery_runs r "
            "ON r.intent_id = o.discovery_intent_id ORDER BY o.sequence LIMIT ?",
            (limit,),
        ).fetchall()
    page = rows[:limit]
    jobs = [stored_job(scope, row[0]) for row in page]
    discovery, usage = DiscoveryStore(root, identity), UsageStore(root, identity)
    groups = []
    for group_id in dict.fromkeys(job["root_import_id"] for job in jobs):
        groups.append(
            {
                **discovery.group(group_id).model_dump(mode="json"),
                "model_usage": usage.selected_summary([("import", [group_id])]),
                "usage_ledger": str(database_path(root)),
            }
        )
    active = {
        "pending",
        "dispatched",
        "started",
        "budget_wait",
        "blocked",
        "interrupted",
        "partial",
        "failed",
        "stopped",
    }
    return {
        "jobs": jobs,
        "groups": groups,
        "checkpoints": [
            dict(
                zip(
                    (
                        "checkpoint_id",
                        "discovery_intent_id",
                        "object_key",
                        "policy",
                        "digest",
                        "import_intent_id",
                        "outcome",
                        "diagnostic",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in checkpoints
        ],
        "next_after": page[-1][1] if len(rows) > limit else None,
        "runnable": sum(
            count for _, status, count in counts if status in {"pending", "dispatched"}
        ),
        "discovery_pending": sum(
            count for kind, status, count in counts if kind == "discovery" and status in active
        ),
        "imports_pending": sum(
            count for kind, status, count in counts if kind == "import" and status in active
        ),
    }
