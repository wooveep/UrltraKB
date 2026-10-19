"""Explicit recovery of native facts, independent of the Wiki working copy."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from ultrakb.config import validate_runtime_config
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.lifecycle import read_lifecycle
from ultrakb.locks import atomic_write_json, file_write_lock
from ultrakb.mutation import RecoveryRequired, recover_pending_journals, repair_marker
from ultrakb.storage.database import open_database
from ultrakb.storage.knowledge_store import KnowledgeStore


@dataclass(frozen=True)
class RepairResult:
    repaired: bool
    recovery: tuple[str, ...]
    issues: tuple[str, ...]
    structural_report: str | None = None
    initialized: bool = True
    projection_status: str | None = None


@dataclass(frozen=True)
class KnowledgeDiagnostic:
    needs_repair: bool
    journals: tuple[str, ...]
    pages: tuple[str, ...]
    structural_report: str | None
    issues: tuple[str, ...]


def inspect_knowledge_base(kb_dir: Path) -> KnowledgeDiagnostic:
    """Read retained facts without rebuilding navigation or replaying recovery."""
    from .storage_lint import preview_stored_lint

    root = kb_dir.expanduser().resolve()
    with open_database(root, read_only=True) as db:
        identity = db.identity
    with read_lifecycle(root, database_identity=identity):
        journals = tuple(sorted(path.name for path in (root / ".ultrakb/journal").glob("*.json")))
        pages: tuple[str, ...] = ()
        issues: list[str] = []
        report = None
        try:
            bound = KnowledgeStore(root, identity).diagnostic_publication()
            pages = tuple(path for path in bound.files() if path.endswith(".md"))
            bound.navigation_revision()
            _, report = preview_stored_lint(KnowledgeScope(root, root / "wiki", knowledge=bound))
        except Exception as exc:
            issues.append(f"Committed knowledge inspection incomplete ({type(exc).__name__})")
        return KnowledgeDiagnostic(
            repair_marker(root).exists() or bool(journals) or bool(issues),
            journals,
            pages,
            report,
            tuple(issues),
        )


def read_diagnostic_page(kb_dir: Path, path: str) -> str:
    """Read a committed page even when its derived navigation needs repair."""
    root = kb_dir.expanduser().resolve()
    with open_database(root, read_only=True) as db:
        identity = db.identity
    if not isinstance(path, str) or not path.endswith(".md"):
        raise ValueError("Invalid diagnostic page path")
    with read_lifecycle(root, database_identity=identity):
        return KnowledgeStore(root, identity).diagnostic_publication().read(path).decode("utf-8")


def repair_global_settings() -> RepairResult:
    """Recover the global settings pair using its own lock and shape checks."""
    from dotenv.parser import parse_stream

    from ultrakb import config
    from ultrakb.application.settings import _read_global_config

    root = config.GLOBAL_CONFIG_DIR
    lock_path = root / "global.lock"
    messages: list[str] = []
    with file_write_lock(lock_path):
        try:
            messages = recover_pending_journals(root, repairing=True, lock_path=lock_path)
            if config.GLOBAL_CONFIG_PATH.exists():
                value = yaml.safe_load(config.GLOBAL_CONFIG_PATH.read_text("utf-8"))
                if value is not None and not isinstance(value, dict):
                    raise ValueError("Global configuration must be a mapping")
                validate_runtime_config(value or {}, allow_inherited=True)
            if (root / ".env").exists():
                with (root / ".env").open(encoding="utf-8") as stream:
                    if any(binding.error for binding in parse_stream(stream)):
                        raise ValueError("Global credential file is invalid")
            _read_global_config()
        except (RecoveryRequired, OSError, ValueError, yaml.YAMLError) as exc:
            atomic_write_json(repair_marker(root), {"error_type": type(exc).__name__})
            return RepairResult(
                False,
                tuple(messages),
                (f"Global settings check failed: {type(exc).__name__}; evidence retained.",),
            )
        repair_marker(root).unlink(missing_ok=True)
        return RepairResult(True, tuple(messages), ())


def repair_knowledge_base(kb_dir: Path) -> RepairResult:
    """Recover independent native facts and report projection state separately."""
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    from .execution_configuration import repair_storage_config
    from .storage_lint import preview_stored_lint

    root = kb_dir.expanduser().resolve()
    # Gate before lock files, recovery writes, or any SQL mutation.
    with open_database(root, read_only=True) as db:
        identity = db.identity
    messages: list[str] = []
    issues: list[str] = []
    report, projection_status = None, None
    with read_lifecycle(root, database_identity=identity):
        try:
            messages.extend(repair_storage_config(root, identity))
        except (RecoveryRequired, OSError, ValueError, yaml.YAMLError) as exc:
            issues.append(f"Settings repair incomplete ({type(exc).__name__}); evidence retained.")
        try:
            receipts = ReceiptStore(root, identity).repair()
            messages.append(f"Repaired response receipts: {receipts.repaired}.")
            if receipts.remaining is None or receipts.remaining:
                issues.append("Response receipts still need repair; remaining evidence retained.")
        except (OSError, ValueError, sqlite3.Error) as exc:
            issues.append(f"Response repair incomplete ({type(exc).__name__}); evidence retained.")
        try:
            store = KnowledgeStore(root, identity)
            bound = store.diagnostic_publication()
            store.repair_navigation(bound.publication_id)
            _, report = preview_stored_lint(KnowledgeScope(root, root / "wiki", knowledge=bound))
        except (OSError, ValueError, sqlite3.Error) as exc:
            issues.append(f"Committed knowledge repair incomplete ({type(exc).__name__}).")
        try:
            projection_status = ProjectionStore(root, identity).sync().status
        except (OSError, ValueError, sqlite3.Error) as exc:
            projection_status = "pending"
            messages.append(
                f"Wiki synchronization pending ({type(exc).__name__}); knowledge retained."
            )
        return RepairResult(
            not issues, tuple(messages), tuple(issues), report, projection_status=projection_status
        )
