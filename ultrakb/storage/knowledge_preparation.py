"""Prepare, seal and verify immutable knowledge candidates outside the final commit."""

import hashlib
import uuid

from pydantic import TypeAdapter

from ultrakb.source_records import RecordId

from .blobs import Blob, put_blob, read_bytes, retain_blob
from .compile_outcomes import CompileOutcome, read_outcome
from .coordination import storage_stage
from .knowledge_inputs import navigation_inputs, prepare_page_inputs, validate_source_paths
from .knowledge_store import BoundKnowledge, PreparedRevision, _batches, _manifest, _path
from .navigation_store import (
    RecoveryRequired,
    bind_navigation,
    prepare_navigation,
    require_navigation,
    resolve_inputs,
)
from .pending_store import PendingWork
from .publication_guards import capture_import_claim, import_claim
from .source_withdrawal import SourceWithdrawal, require_source, withdrawal_manifest
from .wiki_paths import validate_file_tree
from .wiki_projection import prepare_sync_plan, require_sync_plan


def prepare_revision(
    store,
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
) -> PreparedRevision:
    if withdrawal is not None:
        withdrawal = SourceWithdrawal.model_validate(withdrawal)
        if import_work is not None:
            raise ValueError("An import cannot also withdraw a source")
    compile_outcome = CompileOutcome.model_validate(compile_outcome or {})
    encoded_outcome = compile_outcome.model_dump_json()
    if len(encoded_outcome.encode()) > 64 * 1024 or compile_outcome.unfinished:
        raise ValueError("A publication requires a bounded complete compiler outcome")
    if (
        not isinstance(view_id, str)
        or not view_id
        or not isinstance(files, dict)
        or expected is not None
        and (not isinstance(expected, str) or not expected)
    ):
        raise ValueError("A revision requires a view and complete file manifest")
    if len(view_id.encode()) + len((expected or "").encode()) > 63 * 1024:
        raise ValueError("Revision metadata exceeds the encoded storage item limit")
    for path, payload in files.items():
        _path(path)
        if not isinstance(payload, bytes) and not (base is not None and payload is None):
            raise ValueError("Revision content must be immutable bytes")
    if not isinstance(index_revisions, tuple) or len(set(index_revisions)) != len(index_revisions):
        raise ValueError("A revision requires distinct immutable index identities")
    for index_id in index_revisions:
        TypeAdapter(RecordId).validate_python(index_id)
    revision_id, publication_id, plan_id = (uuid.uuid4().hex for _ in range(3))
    with storage_stage(store.root, store.identity) as db:
        if withdrawal is not None:
            require_source(db.connection, withdrawal)
        claim = (
            capture_import_claim(db.connection, store.identity, import_work)
            if import_work is not None
            else None
        )
        inputs = resolve_inputs(db, unit_revisions, index_revisions)
        if claim is not None and not any(unit_id == claim[5] for unit_id, _ in inputs):
            raise ValueError("The published manifest must include the claimed processing unit")
        retained = {}
        if base is not None:
            base._require_publication(db)
            rows = db.connection.execute(
                "SELECT f.path, f.digest, f.byte_count, b.byte_count, b.state "
                "FROM ukb_revision_files f LEFT JOIN ukb_blobs b ON b.digest = f.digest "
                "WHERE f.revision_id = ? ORDER BY f.path",
                (base.publication.revision_id,),
            ).fetchall()
            for path, digest, size, stored_size, state in rows:
                if size != stored_size or state != "live":
                    raise RecoveryRequired("The retained immutable file is unavailable")
                retained[path] = [path, digest, size]
        for path, payload in sorted(files.items()):
            if payload is None:
                retained.pop(path, None)
            else:
                blob = put_blob(db, payload)
                retained[path] = [path, blob.digest, blob.byte_count]
        validate_file_tree(retained)
        entries = [retained[path] for path in sorted(retained)]
        page_rows = prepare_page_inputs(db, entries, inputs, expected, view_id, page_inputs)
        validate_source_paths(db, entries, inputs, page_rows)
        navigation = prepare_navigation(
            store.root,
            store.identity,
            revision_id,
            publication_id,
            entries,
            navigation_inputs(inputs, page_rows),
            primary_units=tuple(unit for unit, _ in inputs),
        )
        manifest = put_blob(
            db,
            _manifest(
                {
                    "files": entries,
                    "inputs": inputs,
                    "navigation": navigation.input_fingerprint,
                    "sync_plan": plan_id,
                    "import_claim": claim,
                    "compile_outcome": compile_outcome.model_dump(mode="json"),
                    "page_inputs": page_rows,
                    "source_withdrawal": withdrawal.model_dump(mode="json") if withdrawal else None,
                }
            ),
        )
        prepared = PreparedRevision(
            revision_id,
            publication_id,
            view_id,
            expected,
            manifest.digest,
            navigation.input_fingerprint,
            plan_id,
            import_work.attempt_id if import_work is not None else None,
        )
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO ukb_knowledge_revisions VALUES (?, ?, ?, ?, ?, ?, 'draft')",
                (
                    revision_id,
                    publication_id,
                    view_id,
                    expected,
                    manifest.digest,
                    navigation.input_fingerprint,
                ),
            )
            retain_blob(connection, "knowledge-revision", revision_id, "manifest", manifest)
            bind_navigation(connection, revision_id, navigation)
            if withdrawal is not None:
                connection.execute(
                    "INSERT INTO ukb_revision_withdrawals VALUES (?, ?, ?, ?)",
                    (
                        revision_id,
                        withdrawal.source_id,
                        withdrawal.source_revision_id,
                        withdrawal.source_generation,
                    ),
                )
            if claim is not None:
                connection.execute(
                    "INSERT INTO ukb_revision_import VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (revision_id, *claim, encoded_outcome),
                )
        prepare_sync_plan(db, plan_id, revision_id, view_id, entries)
        for start in range(0, len(inputs), 63):
            input_batch = inputs[start : start + 63]
            with db.transaction() as connection:
                for unit_id, index_id in input_batch:
                    connection.execute(
                        "INSERT INTO ukb_revision_inputs VALUES (?, ?, ?)",
                        (revision_id, unit_id, index_id),
                    )
        # Each batch is prepared before the short transaction begins.
        for batch in _batches(entries):
            with db.transaction() as connection:
                state = connection.execute(
                    "SELECT state FROM ukb_knowledge_revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()[0]
                if state != "draft":
                    raise ValueError("A sealed revision cannot be changed")
                for path, digest, byte_count in batch:
                    connection.execute(
                        "INSERT INTO ukb_revision_files VALUES (?, ?, ?, ?)",
                        (revision_id, path, digest, byte_count),
                    )
                    retain_blob(
                        connection,
                        "knowledge-revision",
                        revision_id,
                        f"file:{path}",
                        Blob(digest, byte_count),
                    )
        for batch in _batches(page_rows):
            with db.transaction() as connection:
                for path, unit in batch:
                    connection.execute(
                        "INSERT INTO ukb_revision_page_inputs VALUES (?, ?, ?)",
                        (revision_id, path, unit),
                    )
        with db.transaction() as connection:
            connection.execute(
                "UPDATE ukb_knowledge_revisions SET state = 'sealed' "
                "WHERE revision_id = ? AND state = 'draft'",
                (revision_id,),
            )
        # Verify the sealed object without a SQL write transaction or read snapshot.
        persisted = [
            list(row)
            for row in db.connection.execute(
                "SELECT path, digest, byte_count FROM ukb_revision_files "
                "WHERE revision_id = ? ORDER BY path",
                (revision_id,),
            ).fetchall()
        ]
        retained_inputs = db.connection.execute(
            "SELECT unit_revision_id, index_revision_id FROM ukb_revision_inputs "
            "WHERE revision_id = ? ORDER BY unit_revision_id",
            (revision_id,),
        ).fetchall()
        ready_navigation = require_navigation(db, revision_id)
        require_sync_plan(db, revision_id, plan_id)
        sealed_manifest = _manifest(
            {
                "files": persisted,
                "inputs": retained_inputs,
                "navigation": ready_navigation.input_fingerprint,
                "sync_plan": plan_id,
                "import_claim": import_claim(db.connection, revision_id),
                "compile_outcome": read_outcome(db.connection, revision_id).model_dump(mode="json"),
                "source_withdrawal": withdrawal_manifest(db.connection, revision_id),
                "page_inputs": db.connection.execute(
                    "SELECT path, unit_revision_id FROM ukb_revision_page_inputs "
                    "WHERE revision_id = ? ORDER BY path, unit_revision_id",
                    (revision_id,),
                ).fetchall(),
            }
        )
        if hashlib.sha256(sealed_manifest).hexdigest() != prepared.manifest_digest:
            raise ValueError("Sealed revision manifest does not match its entries")
        for path, digest, byte_count in persisted:
            if base is not None and path not in files:
                continue  # Reuse the verified immutable baseline; no full asset rescan.
            if len(read_bytes(db, digest)) != byte_count:
                raise ValueError("Sealed revision content failed validation")
        with db.transaction() as connection:
            updated = connection.execute(
                "UPDATE ukb_knowledge_revisions SET state = 'ready' "
                "WHERE revision_id = ? AND state = 'sealed' AND manifest_digest = ?",
                (revision_id, prepared.manifest_digest),
            ).rowcount
            if updated != 1:
                raise ValueError("Revision changed during sealed validation")
        return prepared
