"""Bounded dispatcher shared by explicit CLI drains and the resident desktop runtime."""

from pathlib import Path


def _main_scope(kb_dir, scope):
    from ultrakb.knowledge_scope import resolve_scope

    selected = resolve_scope(kb_dir, scope)
    if selected.knowledge is None:
        raise ValueError("Pending work requires a main-store knowledge base")
    return selected


def pending_status(kb_dir: Path, *, scope=None, after=0, limit=63) -> dict:
    root = kb_dir.resolve()
    selected = _main_scope(root, scope)
    from .storage_pending_queries import stored_pending_status

    return stored_pending_status(selected, after=after, limit=limit)


def read_pending_job(kb_dir: Path, identity: str, *, scope=None, checkpoint_after=0, limit=63):
    root = kb_dir.resolve()
    selected = _main_scope(root, scope)
    from .storage_pending_queries import stored_job

    return stored_job(
        selected, identity, details=True, checkpoint_after=checkpoint_after, limit=limit
    )


def claim_pending_job(kb_dir: Path, *, scope=None, cancelled=None):
    root = kb_dir.resolve()
    selected = _main_scope(root, scope)
    from .storage_pending import claim_stored_job

    return claim_stored_job(selected, cancelled=cancelled)


def stop_pending_job(kb_dir: Path, identity: str, dispatch_id: str, generation: str, *, scope=None):
    from ultrakb.lifecycle import expected_generation
    from ultrakb.storage.pending_store import PendingStore

    root = kb_dir.resolve()
    with expected_generation(root, generation):
        selected = _main_scope(root, scope)
        return PendingStore(root, selected.knowledge.identity).stop(identity, dispatch_id)


def run_pending_job(kb_dir: Path, identity: str, dispatch_id: str, *, context=None, scope=None):
    root = kb_dir.resolve()
    selected = _main_scope(root, scope)
    from .storage_pending import run_stored_job

    return run_stored_job(selected, identity, dispatch_id, context)


def process_pending(kb_dir: Path, *, max_jobs: int = 10000, context=None, scope=None) -> dict:
    if type(max_jobs) is not int or not 1 <= max_jobs <= 10000:
        raise ValueError("Choose 1–10000 jobs per drain")
    selected = _main_scope(kb_dir.resolve(), scope)
    from .storage_pending import process_stored_pending

    return process_stored_pending(selected, max_jobs=max_jobs, context=context)


def retry_pending_job(kb_dir: Path, identity: str, *, scope=None) -> None:
    root = kb_dir.resolve()
    selected = _main_scope(root, scope)
    from ultrakb.storage.pending_store import PendingStore

    PendingStore(root, selected.knowledge.identity).retry(identity)
    return


def update_execution_budget(kb_dir: Path, identity: str, patch: dict, *, scope=None) -> dict:
    root = kb_dir.resolve()
    selected = _main_scope(root, scope)
    from ultrakb.storage.discovery_store import DiscoveryStore

    return DiscoveryStore(root, selected.knowledge.identity).update_budget(identity, patch)


def cancel_execution_group(kb_dir: Path, identity: str, *, scope=None) -> None:
    root = kb_dir.resolve()
    selected = _main_scope(root, scope)
    from ultrakb.storage.pending_store import PendingStore

    PendingStore(root, selected.knowledge.identity).cancel(identity)
    return
