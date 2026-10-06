"""Recompile admitted units using their committed normalized inputs only."""

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

from openkb.application.execution import ExecutionContext
from openkb.application.ingestion import result_from_publication
from openkb.application.sources import _manifest
from openkb.compilation_report import collect_compile_report
from openkb.config import DEFAULT_CONFIG, resolve_concurrency, resolve_effective_config
from openkb.index_packages import copy_index_package
from openkb.ingest_records import UnitRevision
from openkb.ingest_result import IngestResult
from openkb.knowledge_scope import KnowledgeScope, live_scope
from openkb.llm_usage_execution import track_import_usage
from openkb.locks import LockCancelled
from openkb.mutation import RecoveryRequired, _copy_file_atomic, mutation_scope
from openkb.source_catalog import Admission, read_record, read_source, read_source_revision
from openkb.source_records import DiscoveryIntent
from openkb.unit_publication import (
    begin_unit_attempt,
    copy_tree,
    list_source_units,
    prepare_compile_view,
    publish_unit_revision,
    read_unit_publication,
    record_unit_failure,
)


@track_import_usage
async def recompile_source(
    kb_dir: Path,
    source_id: str,
    *,
    context: ExecutionContext | None = None,
    bundle=None,
    model: str | None = None,
    max_concurrency: int | None = None,
    scope: KnowledgeScope | None = None,
    unit_id: str | None = None,
) -> IngestResult:
    """Caller holds the KB write lease and checks the review precondition."""
    from openkb.agent import compiler

    source = read_source(kb_dir, source_id)
    from openkb.application.sources import source_view_id

    scope = scope or live_scope(kb_dir, source_view_id(kb_dir, source))
    units = list_source_units(kb_dir, source_id)
    if source.removed or not units:
        return IngestResult(
            source.identity,
            "blocked",
            (),
            source_id=source_id,
            message="No retained processing unit is available",
        )
    if unit_id is None and len(units) > 1:
        from openkb.application.workbook_ingestion import aggregate_units

        results = []
        for selected_unit in units:
            results.append(
                await recompile_source(
                    kb_dir,
                    source_id,
                    unit_id=selected_unit.unit_id,
                    context=context,
                    bundle=bundle,
                    model=model,
                    max_concurrency=max_concurrency,
                    scope=scope,
                )
            )
        return aggregate_units(results)
    unit = next((item for item in units if item.unit_id == unit_id), None) if unit_id else units[0]
    if unit is None:
        raise ValueError("Processing unit does not belong to this source")
    try:
        previous = read_unit_publication(kb_dir, unit.unit_id, scope.view_id)
    except FileNotFoundError:
        previous = None
    revision = read_record(
        kb_dir,
        "unit-revisions",
        previous.target_revision_id if previous else unit.target_revision_id,
        UnitRevision,
    )
    frozen = read_source_revision(kb_dir, revision.source_revision_id)
    intent = read_record(kb_dir, "discovery-intents", frozen.discovery_intent_id, DiscoveryIntent)
    admission = Admission(source, frozen, intent)
    if frozen.original_kind == "original":
        from openkb.application.retained_inputs import frozen_source_input
        from openkb.import_text import (
            ImportTextRejected,
            preflight_import_text,
            rejection_result,
            validate_text_preflight,
        )

        prepared = frozen_source_input(kb_dir, source, frozen)
        try:
            validate_text_preflight(
                prepared,
                preflight_import_text(
                    kb_dir, prepared, check_stop=context.check_stop if context else lambda: None
                ),
            )
        except ImportTextRejected as exc:
            return replace(
                rejection_result(prepared, exc),
                source_id=source.source_id,
                source_revision_id=frozen.source_revision_id,
            )
    if previous and previous.status in {"empty", "retired"}:
        return result_from_publication(kb_dir, admission, previous, status="skipped")
    actual = _manifest(kb_dir, previous) if previous else None
    source_map = None
    processing = None
    if actual and previous and previous.successful_revision_id == revision.unit_revision_id:
        directory, manifest = actual
        normalized_source = manifest.normalized_source
        normalized_format = manifest.normalized_format
        source_map = manifest.source_map
        processing = manifest.processing
        if source_map:
            from openkb.source_map import read_source_map

            read_source_map(directory / "wiki", source_map, unit.doc_name)
        index_ref = manifest.index_ref
        is_long = manifest.execution_mode == "segmented"
    elif source.legacy_hash and (previous is None or previous.successful_revision_id is None):
        from openkb.legacy_sources import legacy_snapshot

        directory, legacy = legacy_snapshot(kb_dir, source)
        normalized_source, index_ref = legacy.normalized_source, legacy.index_ref
        is_long = index_ref is not None
        normalized_format = "pdf" if is_long else "markdown"
    elif previous:
        blocked = previous.model_copy(
            update={
                "message": previous.message
                or "No saved normalization for the target; retry its import",
            }
        )
        return result_from_publication(kb_dir, admission, blocked, status="blocked")
    else:
        return IngestResult(
            source.identity,
            "blocked",
            (),
            source_id=source_id,
            message="No saved normalization for this input",
        )
    if normalized_source:
        from openkb.import_text import ImportTextRejected, require_normalized_text

        try:
            require_normalized_text(directory / "wiki" / normalized_source)
        except ImportTextRejected as exc:
            return IngestResult(
                source.identity,
                "rejected",
                (),
                source_id=source_id,
                source_revision_id=frozen.source_revision_id,
                message=str(exc),
                quality=("import_text_rejected",),
                unfinished=("text_preflight",),
            )
    state, runnable = begin_unit_attempt(
        kb_dir, unit, revision, recompile=True, discovery_intent=intent, view_id=scope.view_id
    )
    if not runnable:
        return result_from_publication(kb_dir, admission, state, status="blocked")
    check_stop = context.check_stop if context else lambda: None
    from openkb.llm_usage import usage_context

    try:
        from openkb.conversion_artifacts import read_conversion_artifacts
        from openkb.office.records import OfficeConversion
        from openkb.office.slide_content import require_slide_navigation

        conversion = read_conversion_artifacts(
            kb_dir, revision.unit_revision_id, frozen.source_revision_id
        )
        if conversion.get("office"):
            require_slide_navigation(OfficeConversion.model_validate(conversion["office"]).slides)
        with (
            usage_context(
                source_id=source.source_id,
                source_revision_id=frozen.source_revision_id,
                unit_id=unit.unit_id,
                unit_revision_id=revision.unit_revision_id,
                attempt_id=state.job_id,
                root_import_id=intent.root_import_id,
            ),
            context.begin(kb_dir) if context else nullcontext(bundle) as credentials,
            collect_compile_report() as report,
            prepare_compile_view(kb_dir, scope.view_id) as view,
        ):
            config = resolve_effective_config(kb_dir)[0]
            options: dict[str, Any] = {
                "bundle": credentials,
                "scope": view.scope,
                "max_concurrency": max_concurrency
                or resolve_concurrency(config)
                or compiler.DEFAULT_COMPILE_CONCURRENCY,
            }
            model = model or config.get("model", DEFAULT_CONFIG["model"])
            if normalized_source is None:
                raise ValueError("Retained normalization is missing its source path")
            with mutation_scope(kb_dir, [view.scope.wiki_dir.parent], operation="recompile-unit"):
                normalized = view.scope.wiki_dir / normalized_source
                _copy_file_atomic(directory / "wiki" / normalized_source, normalized)
                if source_map and source_map.path != normalized_source:
                    _copy_file_atomic(
                        directory / "wiki" / source_map.path, view.scope.wiki_dir / source_map.path
                    )
                assets = directory / "wiki/sources/images" / unit.doc_name
                if assets.exists():
                    copy_tree(assets, view.scope.wiki_dir / "sources/images" / unit.doc_name)
                check_stop()
                if is_long:
                    if index_ref is None:
                        raise ValueError("Retained segmented source has no index reference")
                    copy_index_package(directory / "index", view.scope.wiki_dir.parent / "index")
                    summary = view.scope.wiki_dir / "summaries" / f"{unit.doc_name}.md"
                    _copy_file_atomic(directory / "wiki/summaries" / summary.name, summary)
                    await compiler.compile_long_doc(
                        unit.doc_name, summary, index_ref, kb_dir, model, **options
                    )
                else:
                    await compiler.compile_short_doc(
                        unit.doc_name, normalized, kb_dir, model, **options
                    )
                check_stop()
            published = publish_unit_revision(
                kb_dir,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=normalized_source,
                source_map=source_map,
                processing=processing,
                is_long=is_long,
                index_ref=index_ref,
                normalized_format=normalized_format or "markdown",
                check_stop=check_stop,
            )
        result = result_from_publication(
            kb_dir,
            admission,
            published,
            status="added" if published.status == "completed" else "blocked",
        )
        return replace(
            result,
            quality=tuple(dict.fromkeys((*result.quality, *report.quality))),
            unfinished=result.unfinished + tuple(report.unfinished),
        )
    except RecoveryRequired:
        raise
    except Exception as exc:
        failed = record_unit_failure(kb_dir, state, "compilation", exc, discovery_intent=intent)
        if isinstance(exc, LockCancelled):
            raise
        return result_from_publication(kb_dir, admission, failed, status="failed")
