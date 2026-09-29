"""Rebuild generated knowledge from exact retained inputs of one view."""

from pathlib import Path

from openkb.application.sources import _manifest
from openkb.compilation_report import collect_compile_report
from openkb.config import DEFAULT_CONFIG, resolve_concurrency, resolve_effective_config
from openkb.ingest_records import ImportUnit, UnitRevision
from openkb.knowledge_scope import KnowledgeScope
from openkb.mutation import _copy_file_atomic
from openkb.source_catalog import read_record, read_source_revision
from openkb.unit_publication import copy_tree, read_unit_publication


async def compile_refresh_inputs(
    kb_dir: Path, scope: KnowledgeScope, inputs: dict[str, str], bundle, check_stop
) -> tuple[str, ...]:
    from openkb.agent import compiler

    config = resolve_effective_config(kb_dir)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    originals = set()
    with collect_compile_report() as report:
        for unit_id, identity in inputs.items():
            check_stop()
            unit = read_record(kb_dir, "units", unit_id, ImportUnit)
            revision = read_record(kb_dir, "unit-revisions", identity, UnitRevision)
            state = read_unit_publication(kb_dir, unit_id, scope.view_id)
            retained = _manifest(kb_dir, state)
            if state.successful_revision_id != identity or retained is None:
                raise ValueError(
                    "Current input has no retained normalization; retry its import first"
                )
            directory, manifest = retained
            if manifest.normalized_source is None:
                raise ValueError("Retained input has no normalized source")
            frozen = read_source_revision(kb_dir, revision.source_revision_id)
            originals.add(frozen.original)
            originals.update(asset.artifact for asset in frozen.assets if asset.artifact)
            normalized = scope.wiki_dir / manifest.normalized_source
            _copy_file_atomic(directory / "wiki" / manifest.normalized_source, normalized)
            if manifest.source_map:
                from openkb.source_map import read_source_map

                read_source_map(directory / "wiki", manifest.source_map, unit.doc_name)
                if manifest.source_map.path != manifest.normalized_source:
                    _copy_file_atomic(
                        directory / "wiki" / manifest.source_map.path,
                        scope.wiki_dir / manifest.source_map.path,
                    )
            images = f"sources/images/{unit.doc_name}"
            if (directory / "wiki" / images).exists():
                copy_tree(directory / "wiki" / images, scope.wiki_dir / images)
            options = {
                "scope": scope,
                "bundle": bundle,
                "max_concurrency": resolve_concurrency(config)
                or compiler.DEFAULT_COMPILE_CONCURRENCY,
            }
            if manifest.execution_mode == "segmented":
                if not manifest.index_ref:
                    raise ValueError("Segmented input has no retained index")
                copy_tree(directory / "index", scope.wiki_dir.parent / "index")
                summary = scope.wiki_dir / "summaries" / f"{unit.doc_name}.md"
                _copy_file_atomic(directory / "wiki/summaries" / summary.name, summary)
                await compiler.compile_long_doc(
                    unit.doc_name, summary, manifest.index_ref, kb_dir, model, **options
                )
            else:
                await compiler.compile_short_doc(
                    unit.doc_name, normalized, kb_dir, model, **options
                )
            check_stop()
        if report.unfinished:
            raise ValueError("Refresh incomplete: " + ", ".join(report.unfinished))
    return tuple(sorted(originals))
