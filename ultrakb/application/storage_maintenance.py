"""Shared explicit maintenance commands, independent of model configuration."""

from dataclasses import dataclass
from pathlib import Path

from ultrakb.knowledge_scope import resolve_scope
from ultrakb.storage.database import DatabaseIdentity, open_database
from ultrakb.storage.diagnostic_retention import DiagnosticRetention, DiagnosticRetentionPreview
from ultrakb.storage.execution_store import now
from ultrakb.storage.maintenance import CollectionPreview, Maintenance
from ultrakb.storage.observation_retention import ObservationRetention, ObservationRetentionPreview
from ultrakb.storage.receipt_store import ReceiptStore
from ultrakb.storage.root_retirement import RootRetirement, RootRetirementPreview


@dataclass(frozen=True)
class BackupResult:
    status: str
    path: str
    identity: DatabaseIdentity
    coverage: dict[str, str]


@dataclass(frozen=True)
class MaintenanceOverview:
    schema_version: int
    statistics_at: str
    kb_id: str
    generation: str
    roots: RootRetirementPreview
    garbage: CollectionPreview
    observations: ObservationRetentionPreview
    diagnostics: DiagnosticRetentionPreview


def backup_knowledge(kb_dir: Path, destination: Path, *, scope=None, identity=None) -> BackupResult:
    receipt = _maintenance(kb_dir, scope, identity).backup_package(destination)
    return BackupResult("completed", str(destination.resolve()), receipt.identity, receipt.coverage)


def restore_knowledge(backup: Path, destination: Path) -> BackupResult:
    receipt = Maintenance.restore_package(backup, destination)
    return BackupResult("completed", str(destination.resolve()), receipt.identity, receipt.coverage)


def _maintenance(kb_dir, scope, identity=None):
    if scope is None:
        with open_database(kb_dir, expected=identity, read_only=True) as db:
            return Maintenance(db.root, db.identity)
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("Collection requires a main-store knowledge base")
    if identity is not None and scope.knowledge.identity != identity:
        raise ValueError("Maintenance belongs to another knowledge-base generation")
    return Maintenance(scope.kb_dir, scope.knowledge.identity)


def preview_collection(
    kb_dir: Path,
    *,
    scope=None,
    identity=None,
    max_items=128,
    max_bytes=64 * 1024**2,
    cancelled=None,
):
    return _maintenance(kb_dir, scope, identity).preview(
        max_items=max_items, max_bytes=max_bytes, cancelled=cancelled
    )


def collect_garbage(
    kb_dir: Path, *, scope=None, identity=None, max_items=128, max_bytes=64 * 1024**2
):
    return _maintenance(kb_dir, scope, identity).collect(max_items=max_items, max_bytes=max_bytes)


def preview_root_retirement(
    kb_dir: Path,
    *,
    scope=None,
    identity=None,
    max_items=128,
    max_bytes=256 * 1024,
    cancelled=None,
):
    maintenance = _maintenance(kb_dir, scope, identity)
    return RootRetirement(maintenance.root, maintenance.identity).preview(
        max_items=max_items, max_bytes=max_bytes, cancelled=cancelled
    )


def retire_unused_roots(
    kb_dir: Path, *, scope=None, identity=None, max_items=128, max_bytes=256 * 1024
):
    maintenance = _maintenance(kb_dir, scope, identity)
    return RootRetirement(maintenance.root, maintenance.identity).retire(
        max_items=max_items, max_bytes=max_bytes
    )


def preview_storage_maintenance(
    kb_dir: Path,
    *,
    scope=None,
    identity=None,
    max_items=128,
    max_bytes=64 * 1024**2,
    cancelled=None,
) -> MaintenanceOverview:
    maintenance = _maintenance(kb_dir, scope, identity)
    roots = RootRetirement(maintenance.root, maintenance.identity).preview(
        max_items=max_items, max_bytes=min(max_bytes, 256 * 1024), cancelled=cancelled
    )
    garbage = maintenance.preview(max_items=max_items, max_bytes=max_bytes, cancelled=cancelled)
    observations = ObservationRetention(maintenance.root, maintenance.identity).preview(
        max_items=max_items, max_bytes=max_bytes, cancelled=cancelled
    )
    diagnostics = DiagnosticRetention(maintenance.root, maintenance.identity).preview(
        max_items=max_items, max_bytes=max_bytes, cancelled=cancelled
    )
    return MaintenanceOverview(
        1,
        now(),
        maintenance.identity.kb_id,
        maintenance.identity.generation,
        roots,
        garbage,
        observations,
        diagnostics,
    )


def archive_task_observations(
    kb_dir: Path, *, scope=None, identity=None, max_items=128, max_bytes=256 * 1024, cancelled=None
):
    """Archive only expired optional stage details, preserving all core task facts."""
    maintenance = _maintenance(kb_dir, scope, identity)
    return ObservationRetention(maintenance.root, maintenance.identity).archive(
        max_items=max_items, max_bytes=max_bytes, cancelled=cancelled
    )


def archive_compiler_diagnostics(
    kb_dir: Path,
    *,
    scope=None,
    identity=None,
    max_items=128,
    max_bytes=64 * 1024**2,
    cancelled=None,
):
    """Retire only unused diagnostic copies; provider receipts and recovery stay intact."""
    maintenance = _maintenance(kb_dir, scope, identity)
    return DiagnosticRetention(maintenance.root, maintenance.identity).archive(
        max_items=max_items, max_bytes=max_bytes, cancelled=cancelled
    )


def repair_knowledge_receipts(
    kb_dir: Path,
    *,
    scope=None,
    identity=None,
    max_items=128,
    max_bytes=64 * 1024**2,
):
    maintenance = _maintenance(kb_dir, scope, identity)
    return ReceiptStore(maintenance.root, maintenance.identity).repair(
        limit=max_items, byte_limit=max_bytes
    )


def audit_knowledge_usage(
    kb_dir: Path,
    *,
    scope=None,
    identity=None,
    max_items=128,
    max_bytes=256 * 1024,
    cancelled=None,
):
    from ultrakb.storage.usage_audit import UsageAudit

    maintenance = _maintenance(kb_dir, scope, identity)
    return UsageAudit(maintenance.root, maintenance.identity).run(
        max_items=max_items, max_bytes=max_bytes, cancelled=cancelled
    )


def repair_knowledge_usage(
    kb_dir: Path,
    *,
    scope=None,
    identity=None,
    max_items=128,
    max_bytes=256 * 1024,
    cancelled=None,
):
    from ultrakb.storage.usage_audit import UsageAudit

    maintenance = _maintenance(kb_dir, scope, identity)
    return UsageAudit(maintenance.root, maintenance.identity).run(
        repair=True, max_items=max_items, max_bytes=max_bytes, cancelled=cancelled
    )
