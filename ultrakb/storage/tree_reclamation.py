"""Retire only unbound trees, with bounded row and byte work per exclusive stage."""

import json


def tree_candidates(db, max_items):
    return db.connection.execute(
        "SELECT tree_id FROM ukb_tree_builds b WHERE NOT EXISTS "
        "(SELECT 1 FROM ukb_tree_refs r WHERE r.tree_id = b.tree_id) AND NOT EXISTS "
        "(SELECT 1 FROM ukb_index_revisions i WHERE i.tree_id = b.tree_id) "
        "ORDER BY tree_id LIMIT ?",
        (max_items,),
    ).fetchall()


def reclaim_trees(db, *, max_items: int, max_rows: int = 128, max_bytes: int = 256 * 1024):
    candidates = tree_candidates(db, max_items)
    deleted = rows_deleted = encoded_bytes = 0
    for (tree_id,) in candidates:
        with db.transaction() as connection:
            changed = connection.execute(
                "UPDATE ukb_tree_builds SET state = 'retiring' WHERE tree_id = ? AND NOT EXISTS "
                "(SELECT 1 FROM ukb_tree_refs WHERE tree_id = ?) AND NOT EXISTS "
                "(SELECT 1 FROM ukb_index_revisions WHERE tree_id = ?)",
                (tree_id, tree_id, tree_id),
            ).rowcount
        if not changed:
            continue
        for table, ordering in (("nodes", "depth DESC, node_id"), ("entities", "entity_id")):
            remaining = max_rows - rows_deleted
            if remaining <= 0:
                return deleted, rows_deleted
            raw = db.connection.execute(
                f"SELECT rowid, * FROM {table} WHERE tree_id = ? ORDER BY {ordering} LIMIT ?",
                (tree_id, min(63, remaining)),
            ).fetchall()
            batch = []
            for row in raw:
                slot = f"{'entity' if table == 'entities' else 'node'}:{row[2]}"
                size = len(
                    json.dumps((row, ("tree-build", tree_id, slot)), ensure_ascii=False).encode()
                )
                if encoded_bytes + size > max_bytes:
                    break
                batch.append(row)
                encoded_bytes += size
            with db.transaction() as connection:
                for row in batch:
                    connection.execute(
                        f"DELETE FROM {table} WHERE tree_id = ? AND rowid = ?", (tree_id, row[0])
                    )
                    connection.execute(
                        "DELETE FROM ukb_blob_refs WHERE owner_kind = 'tree-build' "
                        "AND owner_id = ? AND slot = ?",
                        (tree_id, f"{'entity' if table == 'entities' else 'node'}:{row[2]}"),
                    )
            rows_deleted += len(batch)
            if (
                len(batch) < len(raw)
                or db.connection.execute(
                    f"SELECT 1 FROM {table} WHERE tree_id = ? LIMIT 1",
                    (tree_id,),
                ).fetchone()
            ):
                return deleted, rows_deleted
        with db.transaction() as connection:
            # Node and entity content references were removed with their bounded rows.
            connection.execute(
                "DELETE FROM ukb_blob_refs WHERE owner_kind = 'tree-build' "
                "AND owner_id = ? AND slot = 'manifest'",
                (tree_id,),
            )
            connection.execute("DELETE FROM ukb_tree_builds WHERE tree_id = ?", (tree_id,))
            connection.execute("DELETE FROM trees WHERE tree_id = ?", (tree_id,))
        deleted += 1
    return deleted, rows_deleted
