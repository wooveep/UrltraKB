"""Shared explicit maintenance commands, independent of model configuration."""

from dataclasses import dataclass
from pathlib import Path

from ultrakb.knowledge_scope import resolve_scope
from ultrakb.storage.database import DatabaseIdentity, open_database
from ultrakb.storage.maintenance import Maintenance
from ultrakb.storage.root_retirement import RootRetirement


@dataclass(frozen=True)
class BackupResult:
    status: str
    path: str
    identity: DatabaseIdentity
    coverage: dict[str, str]


def backup_knowledge(kb_dir: Path, destination: Path, *, scope=None) -> BackupResult:
    receipt = _maintenance(kb_dir, scope).backup_package(destination)
    return BackupResult("completed", str(destination.resolve()), receipt.identity, receipt.coverage)


def restore_knowledge(backup: Path, destination: Path) -> BackupResult:
    receipt = Maintenance.restore_package(backup, destination)
    return BackupResult("completed", str(destination.resolve()), receipt.identity, receipt.coverage)


def _maintenance(kb_dir, scope):
    if scope is None:
        with open_database(kb_dir, read_only=True) as db:
            return Maintenance(db.root, db.identity)
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("Collection requires a main-store knowledge base")
    return Maintenance(scope.kb_dir, scope.knowledge.identity)


def preview_collection(kb_dir: Path, *, scope=None, max_items=128, max_bytes=64 * 1024**2):
    return _maintenance(kb_dir, scope).preview(max_items=max_items, max_bytes=max_bytes)


def collect_garbage(kb_dir: Path, *, scope=None, max_items=128, max_bytes=64 * 1024**2):
    return _maintenance(kb_dir, scope).collect(max_items=max_items, max_bytes=max_bytes)


def preview_root_retirement(kb_dir: Path, *, scope=None, max_items=128, max_bytes=256 * 1024):
    maintenance = _maintenance(kb_dir, scope)
    return RootRetirement(maintenance.root, maintenance.identity).preview(
        max_items=max_items, max_bytes=max_bytes
    )


def retire_unused_roots(kb_dir: Path, *, scope=None, max_items=128, max_bytes=256 * 1024):
    maintenance = _maintenance(kb_dir, scope)
    return RootRetirement(maintenance.root, maintenance.identity).retire(
        max_items=max_items, max_bytes=max_bytes
    )
