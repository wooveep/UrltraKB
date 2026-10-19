"""A restored snapshot retains provenance but cannot keep its former processes alive."""


def interrupt_work(db):
    for table, condition, assignment in (
        (
            "ukb_stage_attempts",
            "state = 'started'",
            "state = 'interrupted', ended_at = NULL, duration_seconds = NULL",
        ),
        (
            "ukb_executions",
            "state IN ('started', 'stopping')",
            "state = 'interrupted', ended_at = NULL, duration_seconds = NULL",
        ),
        (
            "ukb_pending_attempts",
            "status = 'started'",
            "status = 'interrupted', finished_at = NULL, error_code = 'restored_snapshot'",
        ),
        ("ukb_import_intents", "status IN ('dispatched', 'started')", "status = 'interrupted'"),
        (
            "ukb_artifact_heads",
            "applied_digest IS NOT NULL OR pending_revision_id IS NOT NULL "
            "OR pending_base_digest IS NOT NULL",
            "applied_digest = NULL, pending_revision_id = NULL, pending_base_digest = NULL",
        ),
    ):
        # Only a private restoration calls this. Each statement has a fixed row budget.
        while True:
            with db.transaction() as connection:
                changed = connection.execute(
                    f"UPDATE {table} SET {assignment} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} WHERE {condition} LIMIT 128)"
                ).rowcount
            if changed < 128:
                break
    from .usage_store import interrupt_restored_requests

    interrupt_restored_requests(db)


def rebind_imports(db, generation):
    after = ""
    while True:
        rows = db.connection.execute(
            "SELECT root_import_id FROM ukb_import_groups WHERE root_import_id > ? "
            "ORDER BY root_import_id LIMIT 128",
            (after,),
        ).fetchall()
        if not rows:
            return
        with db.transaction() as connection:
            for (root_id,) in rows:
                connection.execute(
                    "UPDATE ukb_import_groups SET generation = ? WHERE root_import_id = ?",
                    (generation, root_id),
                )
        after = rows[-1][0]
