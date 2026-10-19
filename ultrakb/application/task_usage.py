"""Application task summaries are projections of the knowledge base's main ledger."""

from ultrakb.storage.database import open_database
from ultrakb.storage.usage_store import UsageStore


def task_usage_receipt(kb_dir, task_id, *, receipts=(), identity=None):
    with open_database(kb_dir, expected=identity, read_only=True) as db:
        store = UsageStore(db.root, db.identity)
    return store.application_receipt(task_id, receipts=receipts)
