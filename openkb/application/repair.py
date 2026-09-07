"""Controlled recovery: preserve evidence until rollback and checks succeed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from openkb.config import resolve_effective_config, validate_runtime_config
from openkb.lint import find_invalid_frontmatter, run_structural_lint
from openkb.locks import atomic_write_json, kb_repair_lock
from openkb.mutation import RecoveryRequired, recover_pending_journals, repair_marker


@dataclass(frozen=True)
class RepairResult:
    repaired: bool
    recovery: tuple[str, ...]
    issues: tuple[str, ...]
    structural_report: str | None = None


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
