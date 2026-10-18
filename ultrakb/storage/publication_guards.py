"""Constant-size final checks for one unit's import and publication outcome."""

from .catalog_store import CatalogConflict
from .pending_store import PendingStore, PendingWork, _current


def claim_identity(identity, work: PendingWork):
    if not isinstance(work, PendingWork) or work.kind != "import" or not work.unit_revision_id:
        raise ValueError("An import publication requires an explicit processing-unit claim")
    return (
        work.intent_id,
        work.attempt_id,
        work.dispatch_id,
        identity.generation,
        work.source_revision_id,
        work.unit_revision_id,
        work.root_import_id,
    )


def capture_import_claim(connection, identity, work: PendingWork):
    claim = claim_identity(identity, work)
    current = PendingStore._job(connection, work.intent_id)
    if current != work or current.status != "started":
        raise CatalogConflict("The import claim changed before publication")
    attempt = connection.execute(
        "SELECT generation, status FROM ukb_pending_attempts "
        "WHERE attempt_id = ? AND intent_id = ? AND dispatch_id = ?",
        (work.attempt_id, work.intent_id, work.dispatch_id),
    ).fetchone()
    group = connection.execute(
        "SELECT generation, cancelled FROM ukb_import_groups WHERE root_import_id = ?",
        (work.root_import_id,),
    ).fetchone()
    if attempt != (identity.generation, "started") or group != (identity.generation, 0):
        raise CatalogConflict("The import is stopped, cancelled or belongs to another generation")
    _current(connection, work.source_revision_id, work.unit_revision_id)
    return claim


def import_claim(connection, revision_id):
    return connection.execute(
        "SELECT intent_id, attempt_id, dispatch_id, generation, source_revision_id, "
        "unit_revision_id, root_import_id FROM ukb_revision_import WHERE revision_id = ?",
        (revision_id,),
    ).fetchone()


def require_import_claim(connection, identity, revision_id, attempt_id):
    stored = import_claim(connection, revision_id)
    if attempt_id is None:
        if stored is not None:
            raise ValueError("The exact import claim is required by this candidate")
        return
    if stored is None or stored[1] != attempt_id:
        raise ValueError("The candidate belongs to a different import attempt")
    current = PendingStore._job(connection, stored[0])
    if capture_import_claim(connection, identity, current) != stored:
        raise CatalogConflict("The retained import claim no longer matches its target")


def finish_import_claim(connection, revision_id, publication_id):
    if not connection.in_transaction:
        raise ValueError("Import success belongs to the publication transaction")
    claim = import_claim(connection, revision_id)
    if claim is None:
        return
    updated = connection.execute(
        "UPDATE ukb_pending_attempts SET status = 'completed', publication_id = ?, "
        "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE attempt_id = ? AND status = 'started'",
        (publication_id, claim[1]),
    ).rowcount
    if updated != 1:
        raise CatalogConflict("The import attempt is no longer running")
    updated = connection.execute(
        "UPDATE ukb_import_intents SET status = 'completed' "
        "WHERE intent_id = ? AND status = 'started' AND attempt_id = ? AND dispatch_id = ?",
        (claim[0], claim[1], claim[2]),
    ).rowcount
    if updated != 1:
        raise CatalogConflict("The import dispatch is no longer running")
