"""Shared explicit maintenance commands, independent of model configuration."""

from dataclasses import dataclass
from pathlib import Path

from ultrakb.knowledge_scope import resolve_scope
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.maintenance import Maintenance


@dataclass(frozen=True)
class BackupResult:
    status: str
    path: str
    identity: DatabaseIdentity
    coverage: dict[str, str]


def backup_knowledge(kb_dir: Path, destination: Path, *, scope=None) -> BackupResult:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("Backup requires a main-store knowledge base")
    receipt = Maintenance(scope.kb_dir, scope.knowledge.identity).backup_package(destination)
    return BackupResult("completed", str(destination.resolve()), receipt.identity, receipt.coverage)


def restore_knowledge(backup: Path, destination: Path) -> BackupResult:
    receipt = Maintenance.restore_package(backup, destination)
    return BackupResult("completed", str(destination.resolve()), receipt.identity, receipt.coverage)
