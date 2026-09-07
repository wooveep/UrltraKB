"""Protected Skill rollback and artifact review operations."""

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


async def critique_artifact(kb_dir: Path, path: str) -> Path:
    """Review one existing output with the same lease and recovery as chat."""
    import asyncio

    from openkb.agent.skill_runner import run_skill
    from openkb.artifact_history import preserve_artifact_history
    from openkb.config import DEFAULT_CONFIG, resolve_effective_config
    from openkb.locks import async_kb_lock
    from openkb.model_outputs import model_output_scope

    root = kb_dir.resolve()
    async with async_kb_lock(root / ".openkb", exclusive=True):
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("Critique target must be inside the knowledge base")
        relative = target.relative_to(root)
        if not (
            relative.parts[:1] == ("output",) or relative.parts[:2] == ("wiki", "explorations")
        ):
            raise ValueError("Critique target must be an output or exploration")
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        config = (await asyncio.to_thread(resolve_effective_config, root))[0]
        with preserve_artifact_history(root, target=target), model_output_scope(root):
            await run_skill(
                skill_name="openkb-html-critic",
                intent=f"Critique and patch the HTML file at: {relative}",
                kb_dir=root,
                model=config.get("model", DEFAULT_CONFIG["model"]),
                max_turns=40,
            )
        return target
