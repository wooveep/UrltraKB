"""Retire interrupted draft/sealed preparation, preserving complete candidate roots."""

import json
from dataclasses import dataclass

ELIGIBLE = (
    "r.state IN ('draft', 'sealed', 'retiring') "
    "AND NOT EXISTS (SELECT 1 FROM ukb_publications p WHERE p.revision_id = r.revision_id) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_publication_receipts p WHERE p.revision_id = r.revision_id) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_generation_plans g "
    "WHERE g.knowledge_revision_id = r.revision_id) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_import_candidates c WHERE c.proposal_id = r.revision_id) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_refresh_proposals c WHERE c.proposal_id = r.revision_id) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_lint_candidates c WHERE c.revision_id = r.revision_id) "
    "AND NOT EXISTS (SELECT 1 FROM ukb_sync_plans s JOIN ukb_projection_state p "
    "ON p.active_plan_id = s.plan_id WHERE s.revision_id = r.revision_id)"
)

# Page-input foreign keys precede file deletion; polymorphic roots stay in place
# until all their concrete FK rows are gone. These are this owner's relations.
_RELATIONS = (
    "ukb_revision_page_inputs",
    "ukb_revision_generated",
    "ukb_revision_inputs",
    "ukb_revision_import",
    "ukb_revision_source_changes",
    "ukb_revision_refresh",
    "ukb_revision_files",
    "ukb_revision_navigation",
    "ukb_revision_sync",
)


@dataclass(frozen=True)
class _Deletion:
    sql: str
    parameters: tuple
    reference_bytes: int = 0
    reference: bool = False
    final: bool = False


@dataclass(frozen=True)
class Batch:
    revision_id: str
    operations: tuple[_Deletion, ...]


def select(db, max_items, max_bytes):
    rows = db.connection.execute(
        "SELECT r.revision_id, r.manifest_digest, b.byte_count, r.state "
        "FROM ukb_knowledge_revisions r JOIN ukb_blobs b ON b.digest = r.manifest_digest "
        f"WHERE {ELIGIBLE} ORDER BY r.revision_id LIMIT ?",
        (max_items + 1,),
    ).fetchall()
    batches, spent = [], 0
    for revision_id, manifest, size, state in rows:
        selected, complete = [], True
        marker = state != "retiring"
        if marker:
            cost = len(json.dumps((revision_id, "retiring")).encode()) + 256
            if max_items < 1 or spent + cost > max_bytes:
                return batches, True
            spent += cost
            max_items -= 1

        def append(operation):
            nonlocal spent, max_items
            cost = len(json.dumps((operation.sql, operation.parameters)).encode()) + 256
            if max_items < 1 or spent + cost > max_bytes:
                return False
            selected.append(operation)
            spent += cost
            max_items -= 1
            return True

        for table in _RELATIONS:
            records = db.connection.execute(
                f"SELECT rowid FROM {table} WHERE revision_id = ? ORDER BY rowid LIMIT ?",
                (revision_id, max_items + 1),
            ).fetchall()
            for (rowid,) in records:
                if not append(
                    _Deletion(
                        f"DELETE FROM {table} WHERE rowid = ? AND revision_id = ?",
                        (rowid, revision_id),
                    )
                ):
                    complete = False
                    break
            if not complete:
                break
        if complete:
            plans = db.connection.execute(
                "SELECT p.rowid, p.state, EXISTS (SELECT 1 FROM ukb_blob_refs ref "
                "WHERE ref.owner_kind = 'sync-plan' AND ref.owner_id = p.plan_id) "
                "FROM ukb_sync_plans p WHERE p.revision_id = ? ORDER BY p.rowid LIMIT ?",
                (revision_id, max_items + 1),
            ).fetchall()
            for rowid, state, referenced in plans:
                if (
                    state != "retired"
                    or referenced
                    or not append(
                        _Deletion(
                            "DELETE FROM ukb_sync_plans WHERE rowid = ? AND revision_id = ? "
                            "AND state = 'retired'",
                            (rowid, revision_id),
                        )
                    )
                ):
                    complete = False
                    break
        if complete:
            refs = db.connection.execute(
                "SELECT ref.slot, ref.digest, b.byte_count FROM ukb_blob_refs ref "
                "JOIN ukb_blobs b ON b.digest = ref.digest "
                "WHERE ref.owner_kind = 'knowledge-revision' "
                "AND ref.owner_id = ? AND ref.slot != 'manifest' ORDER BY ref.slot LIMIT ?",
                (revision_id, max_items + 1),
            ).fetchall()
            for slot, digest, byte_count in refs:
                if not append(
                    _Deletion(
                        "DELETE FROM ukb_blob_refs WHERE owner_kind = 'knowledge-revision' "
                        "AND owner_id = ? AND slot = ? AND digest = ?",
                        (revision_id, slot, digest),
                        byte_count,
                        True,
                    )
                ):
                    complete = False
                    break
        if complete:
            refs = db.connection.execute(
                "SELECT slot, tree_id FROM ukb_tree_refs WHERE owner_kind = 'knowledge-revision' "
                "AND owner_id = ? ORDER BY slot LIMIT ?",
                (revision_id, max_items + 1),
            ).fetchall()
            for slot, tree_id in refs:
                if not append(
                    _Deletion(
                        "DELETE FROM ukb_tree_refs WHERE owner_kind = 'knowledge-revision' "
                        "AND owner_id = ? AND slot = ? AND tree_id = ?",
                        (revision_id, slot, tree_id),
                    )
                ):
                    complete = False
                    break
        if complete:
            complete = append(
                _Deletion(
                    "DELETE FROM ukb_knowledge_revisions "
                    "WHERE revision_id = ? AND state = 'retiring'",
                    (revision_id,),
                    size,
                    True,
                    True,
                )
            )
        if selected or marker:
            # A blocked non-retired plan still needs its parent fenced so the
            # existing bounded sync-plan retirement can process it next time.
            batches.append(Batch(revision_id, tuple(selected)))
        if not complete:
            return batches, True
    return batches, len(batches) < len(rows)


def protected_count(db, *, blocked=False):
    return db.connection.execute(
        "SELECT count(*) FROM ukb_knowledge_revisions r "
        + ("" if blocked else f"WHERE NOT ({ELIGIBLE})")
    ).fetchone()[0]


def reference_bytes(batches):
    return sum(operation.reference_bytes for batch in batches for operation in batch.operations)


def retire(db, batch):
    revision_id = batch.revision_id
    with db.transaction() as connection:
        connection.execute(
            "UPDATE ukb_knowledge_revisions AS r SET state = 'retiring' "
            f"WHERE r.revision_id = ? AND r.state IN ('draft', 'sealed') AND {ELIGIBLE}",
            (revision_id,),
        )
        if not connection.execute(
            "SELECT 1 FROM ukb_knowledge_revisions r WHERE r.revision_id = ? "
            f"AND r.state = 'retiring' AND {ELIGIBLE}",
            (revision_id,),
        ).fetchone():
            return 0, 0, 0, 0
        manifest = connection.execute(
            "SELECT manifest_digest FROM ukb_knowledge_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()[0]
    retired = removed = refs = size = 0
    for operation in batch.operations:
        with db.transaction() as connection:
            changed = connection.execute(operation.sql, operation.parameters).rowcount
            if operation.final and changed:
                retired += 1
                changed = connection.execute(
                    "DELETE FROM ukb_blob_refs WHERE owner_kind = 'knowledge-revision' "
                    "AND owner_id = ? AND slot = 'manifest' AND digest = ?",
                    (revision_id, manifest),
                ).rowcount
            if operation.reference:
                refs += changed
                size += operation.reference_bytes if changed else 0
            else:
                removed += changed
    return retired, removed, refs, size
