"""Protected artifact generation, consent, archives, and durable outcome facts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from openkb.agent.skills import PreparedSkill, prepare_skill
from openkb.application.execution import ExecutionContext
from openkb.application.file_state import changed_files, contained_paths, file_versions
from openkb.artifact_history import preserve_artifact_history
from openkb.config import DEFAULT_CONFIG, LlmCredentialBundle, resolve_effective_config
from openkb.locks import LockCancelled, async_kb_lock, kb_read_lock
from openkb.mutation import RecoveryRequired, mutation_scope
from openkb.schema import PAGE_CONTENT_DIRS

if TYPE_CHECKING:
    from openkb.skill.generator import AnyValidationResult

TargetType = Literal["skill", "deck"]


def validate_name(name: str) -> str | None:
    """Existing public Skill/deck slug rules and error messages."""
    if not name:
        return "Skill name must not be empty."
    if len(name) > 64:
        return "Skill name must be at most 64 characters."
    if not all(("a" <= c <= "z") or ("0" <= c <= "9") or c == "-" for c in name):
        return "Skill name must contain only lowercase letters, digits, and dashes."
    if name.startswith("-"):
        return "Skill name must not have a leading dash."
    if name.endswith("-"):
        return "Skill name must not have a trailing dash."
    if "--" in name:
        return "Skill name must not contain consecutive dashes."
    return None


def preflight_generation(kb_dir: Path, name: str) -> str | None:
    error = validate_name(name)
    if error:
        return error
    wiki = kb_dir / "wiki"
    if not wiki.is_dir():
        return "No wiki found in this KB. Run `openkb add <source>` to ingest documents first."
    if not any((wiki / sub).is_dir() and any((wiki / sub).iterdir()) for sub in PAGE_CONTENT_DIRS):
        return (
            "Wiki has no compiled content yet. Ingest at least one "
            "document with `openkb add` first."
        )
    return None


@dataclass(frozen=True)
class GenerationOptions:
    target_type: TargetType
    name: str
    intent: str = field(repr=False)
    overwrite: Literal["refuse", "archive", "overlay"] = "refuse"
    version: str | None = None
    critique: bool = False
    skill_name: str | None = None

    def __post_init__(self) -> None:
        if self.target_type not in {"skill", "deck"}:
            raise ValueError("Unknown artifact type")
        error = validate_name(self.name)
        if error:
            raise ValueError(error)
        if not isinstance(self.intent, str) or not self.intent.strip():
            raise ValueError("Describe the artifact to generate")
        if self.overwrite not in {"refuse", "archive", "overlay"}:
            raise ValueError("Unknown artifact replacement policy")


def _paths(
    kb_dir: Path,
    target_type: TargetType,
    name: str,
    prepared: PreparedSkill | None = None,
) -> tuple[Path, list[Path]]:
    target = kb_dir / "output" / ("skills" if target_type == "skill" else "decks") / name
    if prepared and prepared.output_path:
        output = prepared.output_path
        target = (
            output
            if output.parent in {kb_dir / "output", kb_dir / "wiki/explorations"}
            else output.parent
        )
    # Deck skills can write anywhere in the runner's two allowed zones.
    # Include those zones in both consent and crash recovery, including custom skills.
    roots = (
        [target, kb_dir / ".claude-plugin/marketplace.json"]
        if target_type == "skill"
        else [kb_dir / "output", kb_dir / "wiki/explorations"]
    )
    contained_paths(kb_dir, [target, *roots])
    return target, roots


def _version(kb_dir: Path, roots: list[Path], prepared: PreparedSkill | None = None) -> str:
    files = file_versions(kb_dir, roots)
    # Missing, empty directory, and a regular file are different consent states.
    directories = []
    for root in roots:
        for path in [root, *sorted(root.rglob("*"))] if root.is_dir() else [root]:
            contained_paths(kb_dir, [path])
            if path.is_dir():
                directories.append(path.relative_to(kb_dir).as_posix())
    definition = (
        [prepared.name, prepared.body, prepared.metadata, str(prepared.output_path)]
        if prepared
        else None
    )
    return hashlib.sha256(
        json.dumps([files, directories, definition], sort_keys=True, default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class GenerationPreview:
    target: Path
    exists: bool
    version: str


def preview_generation(
    kb_dir: Path,
    target_type: TargetType,
    name: str,
    *,
    skill_name: str | None = None,
) -> GenerationPreview:
    GenerationOptions(target_type, name, "preview")
    kb_dir = kb_dir.resolve()
    with kb_read_lock(kb_dir / ".openkb"):
        prepared = _prepare(kb_dir, target_type, name, skill_name)
        target, roots = _paths(kb_dir, target_type, name, prepared)
        return GenerationPreview(target, target.exists(), _version(kb_dir, roots, prepared))


def _prepare(kb_dir: Path, target_type: TargetType, name: str, skill_name: str | None):
    if target_type == "deck":
        from openkb.deck import DEFAULT_DECK_SKILL

        return prepare_skill(kb_dir, skill_name or DEFAULT_DECK_SKILL, slug=name)
    return None


@dataclass(frozen=True)
class GenerationResult:
    status: Literal["completed", "failed", "blocked", "conflict", "invalid"]
    output_dir: Path | None = None
    archive_path: Path | None = None
    validation: AnyValidationResult | None = None
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    quality: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    message: str | None = None
    error_type: str | None = None
    artifact_path: Path | None = None


def _archive(kb_dir: Path, target: Path) -> Path:
    workspace = target.with_name(f"{target.name}-workspace")
    contained_paths(kb_dir, [workspace])
    file_versions(kb_dir, [target, workspace])  # Reject escaping links before copying/removing.
    existing = [
        int(p.name.removeprefix("iteration-"))
        for p in workspace.glob("iteration-*")
        if p.name.removeprefix("iteration-").isdigit()
    ]
    dest = workspace / f"iteration-{max(existing, default=0) + 1}"
    with mutation_scope(kb_dir, [target, dest], operation="archive-artifact"):
        if target.is_dir():
            shutil.copytree(target, dest)
            shutil.rmtree(target)
        else:
            dest.mkdir(parents=True)
            shutil.copy2(target, dest / target.name)
            target.unlink()
    return dest


async def generate_artifact(
    kb_dir: Path,
    options: GenerationOptions,
    *,
    context: ExecutionContext | None = None,
    bundle: LlmCredentialBundle | None = None,
    model: str | None = None,
) -> GenerationResult:
    """Hold the complete read/generate/write lease and retain successful stages.

    Old adapters keep their own credential resolution. The desktop begins its
    immutable context only after recovery and consent checks. Ordinary model
    failures commit files already produced; interruption before commit rolls
    back this generation while preserving the independently committed archive.
    """
    kb_dir = kb_dir.resolve()
    async with async_kb_lock(
        kb_dir / ".openkb",
        exclusive=True,
        cancelled=context.cancelled if context else None,
        on_wait=context.waiting if context else None,
    ):
        error = preflight_generation(kb_dir, options.name)
        if error:
            return GenerationResult("invalid", message=error)
        try:
            prepared = _prepare(kb_dir, options.target_type, options.name, options.skill_name)
        except (ValueError, RuntimeError, OSError) as exc:
            return GenerationResult("invalid", message=str(exc), error_type=type(exc).__name__)
        target, roots = _paths(kb_dir, options.target_type, options.name, prepared)
        if options.version is not None and _version(kb_dir, roots, prepared) != options.version:
            return GenerationResult(
                "conflict", message="Artifacts changed; review and confirm again"
            )
        file_target = prepared is not None and prepared.output_path == target
        if target.exists() and (
            options.overwrite == "refuse" or (not file_target and not target.is_dir())
        ):
            return GenerationResult(
                "conflict", message="Artifact already exists; rename or confirm replacement"
            )
        before = file_versions(kb_dir, roots)
        with context.begin(kb_dir) if context else nullcontext(bundle) as credentials:
            from openkb.skill.generator import Generator

            if model is None:
                config = (await asyncio.to_thread(resolve_effective_config, kb_dir))[0]
                model = config.get("model", DEFAULT_CONFIG["model"])
            archive = None
            archive_changes: tuple[str, ...] = ()
            try:
                if target.exists() and options.overwrite == "archive":
                    archive = _archive(kb_dir, target)
                    archive_changes = (f"archived: {archive.relative_to(kb_dir).as_posix()}",)
                gen = Generator(
                    target_type=options.target_type,
                    name=options.name,
                    intent=options.intent,
                    kb_dir=kb_dir,
                    model=model,
                    critique=options.critique,
                    skill_name=options.skill_name,
                    bundle=credentials,
                    **({"prepared": prepared} if prepared is not None else {}),
                )
                failure: Exception | None = None
                quality_diff_failed = False
                if context:
                    context.on_event({"stage": "generating"})
                with (
                    mutation_scope(kb_dir, roots, operation="generate-artifact"),
                    preserve_artifact_history(kb_dir, target=target) as history,
                ):
                    try:
                        await gen.run()
                    except (LockCancelled, RecoveryRequired):
                        raise
                    except Exception as exc:
                        failure = exc
                    if failure is None and archive and options.target_type == "skill":
                        from openkb.skill.workspace import write_diff

                        # Diff is independent and best effort, as in the existing CLI.
                        try:
                            with mutation_scope(
                                kb_dir, [archive / "diff.md"], operation="artifact-diff"
                            ):
                                write_diff(archive, target, archive / "diff.md")
                        except RecoveryRequired:
                            raise
                        except Exception:
                            quality_diff_failed = True
                    changes = archive_changes + changed_files(kb_dir, roots, before)
                    resources = tuple(
                        str(kb_dir / path)
                        for path, version in file_versions(kb_dir, roots).items()
                        if before.get(path) != version
                    )
                    if archive:
                        resources += (str(archive),)
                    artifact_path = prepared.output_path if prepared else target / "SKILL.md"
                    if (
                        artifact_path
                        and artifact_path.is_file()
                        and str(artifact_path) not in resources
                    ):
                        resources += (str(artifact_path),)
                    quality = history.issues + (
                        ["archive_diff_failed"] if quality_diff_failed else []
                    )
                    if gen.validation is not None:
                        if gen.validation.errors:
                            quality.append("validation_errors")
                        if gen.validation.warnings:
                            quality.append("validation_warnings")
                return GenerationResult(
                    "failed" if failure else "completed",
                    target.parent if file_target else target,
                    archive,
                    gen.validation,
                    resources,
                    changes,
                    tuple(quality),
                    (
                        gen.stage
                        if gen.stage in {"generation", "validation", "marketplace"}
                        else "generation",
                    )
                    if failure
                    else (),
                    (f"Generation failed ({type(failure).__name__})" if context else str(failure))
                    if failure
                    else None,
                    type(failure).__name__ if failure else None,
                    artifact_path=artifact_path,
                )
            except (LockCancelled, asyncio.CancelledError):
                raise
            except Exception as exc:
                return GenerationResult(
                    "blocked" if isinstance(exc, RecoveryRequired) else "failed",
                    target,
                    archive,
                    resources=(str(archive),) if archive else (),
                    changes=archive_changes,
                    unfinished=("generation",),
                    error_type=type(exc).__name__,
                    message=f"Generation failed ({type(exc).__name__})" if context else str(exc),
                )
