"""Explicit view refresh: validity is separate from the immutable evidence shown."""

from pathlib import Path

from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.locks import kb_read_lock
from openkb.source_changes import effective_input
from openkb.unit_publication import read_head


def list_knowledge_history(kb_dir: Path, *, scope: KnowledgeScope) -> tuple[dict, ...]:
    from openkb.application.views import view_scope
    from openkb.ingest_records import KnowledgeRevision
    from openkb.unit_publication import wiki_versions

    scope = resolve_scope(kb_dir, scope)
    with kb_read_lock(scope.kb_dir / ".openkb"):
        identity = read_head(scope.kb_dir, scope.view_id).knowledge_revision_id
        seen: set[str] = set()
        history = []
        while identity:
            if identity in seen:
                raise ValueError("Knowledge history contains a cycle")
            seen.add(identity)
            selected = view_scope(scope.kb_dir, scope.view_id, historical_revision=identity)
            manifest = KnowledgeRevision.model_validate_json(
                (selected.wiki_dir.parent / "manifest.json").read_text()
            )
            if manifest.knowledge_revision_id != identity or manifest.view_id != scope.view_id:
                raise ValueError("Knowledge history belongs to another view")
            history.append(
                {
                    "knowledge_revision_id": identity,
                    "change_kind": manifest.change_kind,
                    "input_revisions": manifest.input_revisions,
                    "pages": tuple(
                        p
                        for p in wiki_versions(scope.kb_dir, selected.wiki_dir)
                        if p.endswith(".md") and "/" in p
                    ),
                }
            )
            identity = manifest.base_revision_id
        return tuple(history)


def cleanup_unreferenced_artifacts(kb_dir: Path, candidates: list[str]) -> dict:
    """Clean only explicitly selected managed artifacts, with a complete ownership proof."""
    import shutil

    from pydantic import TypeAdapter

    from openkb.artifact_references import list_artifact_references
    from openkb.file_state import contained_paths
    from openkb.locks import kb_ingest_lock
    from openkb.mutation import mutation_scope
    from openkb.source_records import RelativePath

    kb_dir = kb_dir.resolve()
    with kb_ingest_lock(kb_dir / ".openkb"):
        paths = []
        for candidate in candidates:
            TypeAdapter(RelativePath).validate_python(candidate)
            path = kb_dir / candidate
            if not candidate.startswith((".openkb/artifacts/", ".openkb/normalized/")):
                raise ValueError("Only managed artifacts can be physically cleaned")
            contained_paths(kb_dir, [path])
            if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(kb_dir)):
                raise ValueError("Artifact cleanup cannot follow a symbolic link")
            paths.append(path)
        references = list_artifact_references(kb_dir)
        retained: list[Path] = []
        removed: list[Path] = []
        for path in paths:
            owned = any(p == path or p.is_relative_to(path) for p in references.paths)
            (retained if owned or not references.complete else removed).append(path)
        with mutation_scope(kb_dir, removed, operation="clean-unreferenced-artifacts"):
            for path in removed:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
        return {
            "deleted": [p.relative_to(kb_dir).as_posix() for p in removed],
            "retained": [p.relative_to(kb_dir).as_posix() for p in retained],
            "complete": references.complete,
        }


def read_refresh_proposal(
    kb_dir: Path, identity: str, *, scope: KnowledgeScope | None = None
) -> dict:
    from openkb.refresh_publication import refresh_candidate_view

    kb_dir = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(kb_dir, scope)
    with kb_read_lock(kb_dir / ".openkb"):
        proposal = refresh_candidate_view(kb_dir, identity)
        if scope and (scope.read_only or scope.view_id != proposal["view_id"]):
            raise ValueError("Refresh proposal belongs to another live view")
        return proposal


def list_refresh_proposals(
    kb_dir: Path, *, scope: KnowledgeScope | None = None
) -> tuple[dict, ...]:
    kb_dir = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(kb_dir, scope)
    with kb_read_lock(kb_dir / ".openkb"):
        proposals = (
            read_refresh_proposal(kb_dir, p.parent.name)
            for p in sorted((kb_dir / ".openkb/refresh-proposals").glob("*/proposal.json"))
        )
        return tuple(p for p in proposals if not scope or p["view_id"] == scope.view_id)


def accept_refresh_proposal(
    kb_dir: Path, identity: str, *, version: str, scope: KnowledgeScope | None = None, context=None
):
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock
    from openkb.refresh_publication import RefreshResult, commit_refresh, read_refresh_candidate
    from openkb.state import HashRegistry

    kb_dir = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(kb_dir, scope, writable=True)
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        directory, proposal = read_refresh_candidate(kb_dir, identity)
        if scope and (scope.read_only or scope.view_id != proposal.view_id):
            raise ValueError("Refresh proposal belongs to another live view")
        if HashRegistry.hash_file(directory / "proposal.json") != version:
            return RefreshResult(
                "conflict", proposal.view_id, proposal_id=identity, message="Saved diff changed"
            )
        with context.begin(kb_dir):
            return commit_refresh(kb_dir, directory, proposal, context.check_stop)


def confirm_empty_source(
    kb_dir: Path,
    source_id: str,
    *,
    generation: int,
    scope: KnowledgeScope | None = None,
    context=None,
):
    """An explicit, generation-bound confirmation excludes this revision's contribution."""
    from openkb.application.execution import ExecutionContext
    from openkb.catalog_schema import CatalogSchema, catalog_schema_path
    from openkb.locks import kb_ingest_lock
    from openkb.mutation import mutation_scope
    from openkb.refresh_publication import RefreshResult
    from openkb.source_catalog import read_source, record_path, write_record
    from openkb.source_changes import source_change_heads, source_view

    kb_dir = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(kb_dir, scope, writable=True)
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        source = read_source(kb_dir, source_id)
        view_id = source_view(kb_dir, source)
        if scope and (scope.read_only or scope.view_id != view_id):
            raise ValueError("Empty confirmation belongs to another live view")
        if source.removed or source.target_generation != generation:
            return RefreshResult(
                "conflict", view_id, message="Source changed; review the current source first"
            )
        from openkb.source_changes import exclude_source_inputs

        changed = exclude_source_inputs(kb_dir, source, "empty", view_id=view_id).model_copy(
            update={"contribution_empty": True, "target_generation": generation + 1}
        )
        records = {
            catalog_schema_path(kb_dir): CatalogSchema(),
            record_path(kb_dir, "sources", source_id): changed,
            **source_change_heads(kb_dir, changed, "empty"),
        }
        with (
            context.begin(kb_dir),
            mutation_scope(kb_dir, list(records), operation="confirm-empty-source"),
        ):
            for path, record in records.items():
                write_record(path, record)
            context.check_stop()
        return RefreshResult(
            "completed",
            view_id,
            message="Empty contribution confirmed; dependent knowledge needs refresh",
        )


async def refresh_knowledge_view(kb_dir: Path, *, scope: KnowledgeScope, context=None):
    import shutil

    from openkb.application.execution import ExecutionContext
    from openkb.locks import LockCancelled, async_kb_lock, atomic_write_text
    from openkb.mutation import RecoveryRequired, mutation_scope
    from openkb.refresh_publication import RefreshResult, publish_refresh
    from openkb.schema import AGENTS_MD, INDEX_SEED
    from openkb.source_catalog import list_sources
    from openkb.unit_publication import prepare_compile_view

    scope = resolve_scope(kb_dir, scope, writable=True)
    kb_dir = scope.kb_dir
    context = context or ExecutionContext()
    async with async_kb_lock(
        kb_dir / ".openkb", exclusive=True, cancelled=context.cancelled, on_wait=context.waiting
    ):
        try:
            from openkb.source_changes import unpublished_legacy_inputs

            if scope.view_id == "legacy" and (missing := unpublished_legacy_inputs(kb_dir)):
                return RefreshResult(
                    "blocked",
                    scope.view_id,
                    message="Legacy evidence has no published normalization. Complete source "
                    "mapping and recompilation before refreshing: " + ", ".join(missing.values()),
                    unfinished=("legacy_source_migration",),
                )
            with (
                context.begin(kb_dir) as bundle,
                prepare_compile_view(kb_dir, scope.view_id) as view,
            ):
                inputs = {
                    unit: identity
                    for unit, identity in view.head.inputs.items()
                    if effective_input(kb_dir, scope.view_id, identity)
                }
                from openkb.ingest_records import UnitRevision
                from openkb.source_catalog import read_record
                from openkb.source_changes import source_view
                from openkb.workbooks.progress import workbook_progress

                actual = {
                    read_record(kb_dir, "unit-revisions", identity, UnitRevision).source_revision_id
                    for identity in inputs.values()
                }
                sources = list_sources(kb_dir)
                pending_sources = []
                for source in sources:
                    if (
                        source_view(kb_dir, source) != scope.view_id
                        or source.removed
                        or source.contribution_empty
                    ):
                        continue
                    progress = workbook_progress(kb_dir, source, scope.view_id)
                    if progress is not None and not progress.finished:
                        pending_sources.append(source.name)
                    elif source.target_revision_id not in actual and not (
                        progress is not None and progress.empty
                    ):
                        pending_sources.append(source.name)
                if pending_sources:
                    return RefreshResult(
                        "blocked",
                        scope.view_id,
                        message="Finish importing the current sources before refreshing: "
                        + ", ".join(pending_sources),
                        unfinished=("current_source_import",),
                    )
                generations = {s.source_id: s.target_generation for s in sources}
                with mutation_scope(
                    kb_dir, [view.scope.wiki_dir.parent], operation="build-refreshed-view"
                ):
                    from openkb.unit_publication import copy_tree

                    copy_tree(scope.wiki_dir, view.scope.wiki_dir.parent / "base")
                    for section in ("sources", "summaries", "concepts", "entities"):
                        shutil.rmtree(view.scope.wiki_dir / section, ignore_errors=True)
                        (view.scope.wiki_dir / section).mkdir(parents=True)
                    atomic_write_text(view.scope.wiki_dir / "AGENTS.md", AGENTS_MD)
                    atomic_write_text(view.scope.wiki_dir / "index.md", INDEX_SEED)
                    for page in view.head.needs_refresh:
                        if page.startswith(("reports/", "explorations/")):
                            (view.scope.wiki_dir / page).unlink(missing_ok=True)
                    from openkb.refresh_inputs import compile_refresh_inputs

                    originals = await compile_refresh_inputs(
                        kb_dir, view.scope, inputs, bundle, context.check_stop
                    )
                return publish_refresh(
                    kb_dir, view, inputs, originals, generations, context.check_stop
                )
        except (RecoveryRequired, LockCancelled):
            raise
        except Exception as exc:
            from openkb.ingest_diagnostics import failure_reason

            context.check_stop()
            return RefreshResult(
                "failed", scope.view_id, message=failure_reason(exc), unfinished=("refresh",)
            )


def refresh_status(kb_dir: Path, *, scope: KnowledgeScope) -> dict:
    scope = resolve_scope(kb_dir, scope)
    with kb_read_lock(scope.kb_dir / ".openkb"):
        head = read_head(scope.kb_dir, scope.view_id)
        return {
            "view_id": scope.view_id,
            "knowledge_revision_id": head.knowledge_revision_id,
            "generation": head.generation,
            "needs_refresh": head.model_dump(mode="json")["needs_refresh"],
            "effective_inputs": tuple(
                identity
                for identity in head.inputs.values()
                if effective_input(scope.kb_dir, scope.view_id, identity)
            ),
        }
