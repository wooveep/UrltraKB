"""Controlled recovery: preserve evidence until rollback and checks succeed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from openkb.config import resolve_effective_config, validate_runtime_config
from openkb.lint import find_invalid_frontmatter, run_structural_lint
from openkb.locks import atomic_write_json, file_write_lock, kb_repair_lock
from openkb.mutation import RecoveryRequired, recover_pending_journals, repair_marker


@dataclass(frozen=True)
class RepairResult:
    repaired: bool
    recovery: tuple[str, ...]
    issues: tuple[str, ...]
    structural_report: str | None = None


@dataclass(frozen=True)
class KnowledgeDiagnostic:
    needs_repair: bool
    journals: tuple[str, ...]
    pages: tuple[str, ...]
    structural_report: str | None
    issues: tuple[str, ...]


def inspect_knowledge_base(kb_dir: Path) -> KnowledgeDiagnostic:
    """Inspect retained content without replaying or discarding recovery evidence."""
    from openkb.application.file_state import contained_paths

    root = kb_dir.resolve()
    with kb_repair_lock(root / ".openkb"):
        pages = contained_paths(root, sorted((root / "wiki").rglob("*.md")))
        journals = sorted((root / ".openkb/journal").glob("*.json"))
        issues: tuple[str, ...] = ()
        report = None
        try:
            report = run_structural_lint(root)
        except Exception as exc:
            issues = (f"Structural inspection unavailable ({type(exc).__name__})",)
        return KnowledgeDiagnostic(
            repair_marker(root).exists() or bool(journals),
            tuple(path.name for path in journals),
            tuple(path.relative_to(root / "wiki").as_posix() for path in pages),
            report,
            issues,
        )


def read_diagnostic_page(kb_dir: Path, path: str) -> str:
    """Read a contained wiki page while ordinary operations are repair-blocked."""
    root = kb_dir.resolve()
    target = (root / "wiki" / path).resolve()
    if not target.is_relative_to(root / "wiki") or target.suffix != ".md":
        raise ValueError("Invalid diagnostic page path")
    with kb_repair_lock(root / ".openkb"):
        return target.read_text(encoding="utf-8")


def repair_global_settings() -> RepairResult:
    """Recover the global settings pair using its own lock and shape checks."""
    from dotenv.parser import parse_stream

    from openkb import config
    from openkb.application.settings import read_global_config

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
            read_global_config()
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
    """Retry validated journals, checking storage before allowing normal writes.

    Missing/corrupt evidence is never discarded or reconstructed by guessing.
    Restoring a damaged backup remains a deliberate operator action. Ordinary
    lint notes are reported separately from storage/recovery integrity failures.
    """
    root = kb_dir.expanduser().resolve()
    with kb_repair_lock(root / ".openkb"):
        messages: list[str] = []
        try:
            messages = recover_pending_journals(root, repairing=True)
            for directory in (root / "wiki", root / "raw"):
                if not directory.is_dir():
                    raise ValueError(f"Required directory is missing: {directory.name}")
            config = yaml.safe_load((root / ".openkb/config.yaml").read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("Configuration must be a mapping")
            validate_runtime_config(config, allow_inherited=True)
            validate_runtime_config(resolve_effective_config(root)[0])
            registry_file = root / ".openkb/hashes.json"
            if registry_file.exists():
                entries = json.loads(registry_file.read_text(encoding="utf-8"))
                if not isinstance(entries, dict) or any(
                    not isinstance(value, dict) for value in entries.values()
                ):
                    raise ValueError("Document registry must contain document records")
            issues = find_invalid_frontmatter(root / "wiki")
            if issues:
                atomic_write_json(repair_marker(root), {"error_type": "InvalidFrontmatter"})
                return RepairResult(False, tuple(messages), tuple(issues))
            report = run_structural_lint(root)
        except RecoveryRequired:
            return RepairResult(
                False,
                tuple(messages),
                ("Recovery evidence is incomplete or unusable; journal and backups retained.",),
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            atomic_write_json(repair_marker(root), {"error_type": type(exc).__name__})
            # Parser messages can contain credential text. Report the failure
            # category and keep the original files available for local repair.
            return RepairResult(
                False, tuple(messages), (f"Storage check failed: {type(exc).__name__}",)
            )
        repair_marker(root).unlink(missing_ok=True)
        return RepairResult(True, tuple(messages), (), report)
