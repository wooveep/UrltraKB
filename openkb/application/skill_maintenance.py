"""Restore a Skill and its discoverable manifest as one protected operation."""

from pathlib import Path

from openkb.application.file_state import file_versions
from openkb.locks import kb_ingest_lock
from openkb.mutation import mutation_scope
from openkb.skill import skill_dir, skill_workspace_dir, validate_skill_name


def rollback_skill(kb_dir: Path, name: str, *, iteration: int | None = None) -> Path:
    error = validate_skill_name(name)
    if error:
        raise ValueError(error)
    from openkb.skill.marketplace import regenerate_marketplace
    from openkb.skill.workspace import restore_iteration

    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
        paths = [
            skill_dir(root, name),
            skill_workspace_dir(root, name),
            root / ".claude-plugin/marketplace.json",
        ]
        file_versions(root, paths)
        with mutation_scope(root, paths, operation="restore-skill"):
            restored = restore_iteration(root, name, n=iteration)
            regenerate_marketplace(root)
        return restored
