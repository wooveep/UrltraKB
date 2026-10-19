"""Validated generation options and shared native generator entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ultrakb.application.execution import ExecutionContext
from ultrakb.config import LlmCredentialBundle
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.schema import PAGE_CONTENT_DIRS
from ultrakb.skill import validate_skill_name as validate_name

if TYPE_CHECKING:
    from ultrakb.skill.generator import AnyValidationResult


TargetType = Literal["skill", "deck"]


def preflight_generation(
    kb_dir: Path, name: str, *, scope: KnowledgeScope | None = None
) -> str | None:
    error = validate_name(name)
    if error:
        return error
    from ultrakb.application.query_views import resolve_query_views

    selection = resolve_query_views(kb_dir, scope=scope)
    if not selection.views:
        return "No wiki found in this KB. Run `ultrakb add <source>` to ingest documents first."
    if not any(
        path.split("/")[0] in PAGE_CONTENT_DIRS for view in selection.views for path in view.files
    ):
        return (
            "Wiki has no compiled content yet. Ingest at least one "
            "document with `ultrakb add` first."
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


@dataclass(frozen=True)
class GenerationPreview:
    target: Path
    exists: bool
    version: str


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
    generation_id: str | None = None
    artifact_epoch: int | None = None
    projection_pending: bool = False
    publication_id: str | None = None
    candidate_revision_id: str | None = None
    execution_id: str | None = None
    model_usage: dict | None = field(default=None, compare=False, repr=False)


def preview_generation(kb_dir, target_type, name, *, skill_name=None, scope=None):
    from .storage_generators import preview_stored_generation

    GenerationOptions(target_type, name, "preview")
    return preview_stored_generation(
        resolve_scope(kb_dir, scope), target_type, name, skill_name=skill_name
    )


async def generate_artifact(
    kb_dir: Path,
    options: GenerationOptions,
    *,
    scope: KnowledgeScope | None = None,
    context: ExecutionContext | None = None,
    bundle: LlmCredentialBundle | None = None,
    model: str | None = None,
    pin_scope: bool = True,
) -> GenerationResult:
    import asyncio
    from threading import Event

    from ultrakb.context.workers import run_blocking
    from ultrakb.mutation import RecoveryRequired
    from ultrakb.storage.coordination import storage_stage

    from .generation_results import with_generation_usage
    from .generation_scope import previous_generation_execution, restore_generation_scope
    from .storage_generators import generate_stored_artifact
    from .views import published_scope

    context = context or ExecutionContext()
    explicit = pin_scope and scope is not None and scope.publication_id is not None
    stopped, original_cancelled = Event(), context.cancelled
    context.cancelled = lambda: stopped.is_set() or original_cancelled()
    completed = None

    def generate():
        nonlocal completed
        selected = scope or published_scope(kb_dir, cancelled=context.cancelled)
        previous = context.storage_execution
        try:
            with storage_stage(
                selected.kb_dir,
                selected.knowledge.identity,
                read_only=True,
                cancelled=context.cancelled,
            ):
                selected = resolve_scope(kb_dir, selected)
                previous = previous_generation_execution(selected, context)
                selected = restore_generation_scope(selected, options, context, explicit=explicit)
        except RecoveryRequired as error:
            completed = with_generation_usage(
                selected,
                context,
                GenerationResult(
                    "blocked",
                    message=str(error),
                    error_type=type(error).__name__,
                    unfinished=("restore generation inputs",),
                ),
                previous,
            )
            return
        completed = asyncio.run(
            generate_stored_artifact(selected, options, context, bundle=bundle, model=model)
        )
        completed = with_generation_usage(selected, context, completed, previous)

    try:
        await run_blocking(generate, on_cancel=stopped.set)
    except asyncio.CancelledError:
        if completed is None:
            raise
    finally:
        context.cancelled = original_cancelled
    assert completed is not None
    return completed
