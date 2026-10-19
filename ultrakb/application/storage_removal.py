"""Withdraw current source targets without deleting retained knowledge or original bytes."""

import hashlib
import json
from dataclasses import replace

from ultrakb.locks import LockCancelled
from ultrakb.storage.catalog_store import CatalogConflict, CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict
from ultrakb.storage.source_changes import SourceChange, committed_source_change

from .execution import ExecutionContext
from .publications import publish_knowledge_changes, sync_publication
from .removal import RemovalOutcome, RemovalPreview, RemoveAction, RemovePlan, RemoveResult
from .storage_execution import begin_storage_operation
from .storage_inventory import catalog_sources
from .task_stages import task_stage
from .views import storage_scope


def _version(scope, source):
    value = (
        scope.knowledge.identity.kb_id,
        scope.knowledge.identity.generation,
        scope.view_id,
        scope.publication_id,
        source.source_id,
        source.revision_id,
        source.generation,
    )
    digest = hashlib.sha256(json.dumps(value).encode()).hexdigest()
    return f"withdraw-v1:{scope.publication_id}:{digest}"


def _matches(catalog, identifier, *, include_removed=False):
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Document identifier is required")
    matches, partial = [], []
    query = identifier.strip().casefold()
    for source in catalog_sources(catalog, include_removed=include_removed):
        units = catalog.source_units(source.source_id)
        if identifier in {
            source.source_id,
            source.source_key,
            source.label,
            *(unit.doc_name for unit in units),
        }:
            matches.append((source, units))
        elif query in source.label.casefold() or any(
            query in unit.doc_name.casefold() for unit in units
        ):
            partial.append((source, units))
    return matches or partial


def preview_stored_removal(scope, identifier):
    matches = _matches(CatalogStore(scope.kb_dir, scope.knowledge.identity), identifier)
    if not matches:
        return RemovalPreview("not_found")
    if len(matches) > 1:
        return RemovalPreview(
            "multiple", candidates=tuple(source.source_id for source, _ in matches)
        )
    return _preview(scope, *matches[0])


def _preview(scope, source, units):
    name = units[0].doc_name if units else source.label
    wiki = scope.wiki_dir
    plan = RemovePlan(
        name=source.label,
        doc_name=name,
        doc_type="admitted",
        file_hash=source.source_id,
        actions=[
            RemoveAction("WITHDRAW", source.label),
            *(RemoveAction("WITHDRAW", unit.doc_name) for unit in units if unit.key != "body"),
            RemoveAction(
                "RETAIN", "Published knowledge, historical evidence and managed originals"
            ),
        ],
        concept_deletes=[],
        entity_deletes=[],
        raw_path=None,
        cleanup_pageindex=False,
        pageindex_doc_id=None,
        summary_path=wiki / f"summaries/{name}.md",
        source_md=wiki / f"sources/{name}.md",
        source_json=wiki / f"sources/{name}.json",
        images_dir=wiki / f"sources/images/{name}",
    )
    return RemovalPreview("ready", _version(scope, source), plan)


def _removed(preview, publication_id, *, pending=False):
    plan = preview.plan
    result = RemoveResult(
        "removed",
        plan.name,
        plan.doc_name,
        plan.actions,
        [],
        [],
        0,
        0,
        None,
        None,
        "Source withdrawn; dependent knowledge needs refresh. Historical evidence retained.",
    )
    return RemovalOutcome(
        "removed",
        preview,
        result,
        ("wiki",),
        ("synchronize wiki",) if pending else (),
        publication_id,
        pending,
    )


def remove_stored_source(scope, identifier, *, version, context):
    base = scope.knowledge
    context = context or ExecutionContext()
    if context.storage_identity not in (None, base.identity):
        raise ValueError("Source source_change belongs to another database generation")
    context.storage_identity = base.identity
    catalog, store = (
        CatalogStore(scope.kb_dir, base.identity),
        KnowledgeStore(scope.kb_dir, base.identity),
    )
    matches = _matches(catalog, identifier, include_removed=True)
    if len(matches) == 1 and matches[0][0].removed:
        source, units = matches[0]
        recovered_scope = scope
        if isinstance(version, str) and version.startswith("withdraw-v1:"):
            parts = version.split(":", 2)
            if len(parts) != 3:
                return RemovalOutcome("conflict", RemovalPreview("not_found"))
            try:
                recovered_scope = storage_scope(
                    scope.kb_dir, base.identity, publication_id=parts[1], view_id=scope.view_id
                )
            except FileNotFoundError:
                return RemovalOutcome("conflict", RemovalPreview("not_found"))
        retained = committed_source_change(store, recovered_scope.knowledge, source.source_id)
        if retained is not None:
            prepared, claim = retained
            snapshot = replace(source, generation=claim.source_generation, removed=False)
            if (
                source.revision_id == claim.source_revision_id
                and source.generation == claim.source_generation + 1
                and (version is None or version == _version(recovered_scope, snapshot))
            ):
                projection = sync_publication(scope.kb_dir, base.identity, prepared)
                return _removed(
                    _preview(recovered_scope, snapshot, units),
                    prepared.publication_id,
                    pending=projection.status not in {"synced", "obsolete"},
                )
        return RemovalOutcome("conflict", RemovalPreview("not_found"))
    preview = preview_stored_removal(scope, identifier)
    if preview.status != "ready":
        return RemovalOutcome(preview.status, preview)
    if version is not None and version != preview.version:
        return RemovalOutcome("conflict", preview)
    if store.bind(view_id=scope.view_id).publication_id != scope.publication_id:
        return RemovalOutcome("conflict", preview)
    source = catalog.source(preview.plan.file_hash)
    if _version(scope, source) != preview.version or source.removed:
        return RemovalOutcome("conflict", preview)
    claim = SourceChange(
        source_id=source.source_id,
        source_revision_id=source.revision_id,
        source_generation=source.generation,
    )
    result = None
    try:
        with begin_storage_operation(scope.kb_dir, context, "withdraw-source"):
            with task_stage(
                context,
                "source.withdraw.v1",
                root=scope.kb_dir,
                source_revision_id=source.revision_id,
            ):
                try:
                    outcome = publish_knowledge_changes(
                        base,
                        {},
                        source_change=claim,
                        check_cancelled=context.check_stop,
                        execution_id=context.storage_execution.execution_id,
                    )
                except Exception:
                    retained = committed_source_change(store, base, source.source_id)
                    if retained is None:
                        raise
                    prepared, _ = retained
                    result = _removed(preview, prepared.publication_id, pending=True)
                else:
                    if outcome.status != "published":
                        context._storage_result = "failed"
                        return RemovalOutcome("conflict", preview)
                    result = _removed(
                        preview,
                        outcome.publication.publication_id,
                        pending=outcome.projection.status not in {"synced", "obsolete"},
                    )
                context._storage_result = "completed"
    except (CatalogConflict, PublicationConflict):
        return RemovalOutcome("conflict", preview)
    except Exception as error:
        if result is None:
            from ultrakb.ingest_diagnostics import failure_reason

            return RemovalOutcome(
                "stopped" if isinstance(error, LockCancelled) else "blocked",
                preview,
                unfinished=("withdraw source",),
                message=failure_reason(error),
                error_type=type(error).__name__,
            )
        result = replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
    return result
