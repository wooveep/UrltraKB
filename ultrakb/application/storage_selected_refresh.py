"""Refresh accepted summaries and shared pages without document conversion or planning."""

import hashlib
from dataclasses import dataclass

from ultrakb import frontmatter
from ultrakb.agent.link_registry import FrozenLinkRegistry
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.compilation_report import compilation_attempt
from ultrakb.lint import list_existing_wiki_targets
from ultrakb.llm_usage import usage_context
from ultrakb.selected_refresh import _full_windows, _refresh_index, reconcile_page
from ultrakb.storage.blobs import read_bytes
from ultrakb.storage.catalog_store import CatalogStore, UnitTarget
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.compile_binding import compile_binding
from ultrakb.storage.compile_inputs import materialize_inputs
from ultrakb.storage.compile_outcomes import CompileOutcome
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.knowledge_inputs import source_paths
from ultrakb.storage.knowledge_store import KnowledgeStore

from .task_stages import task_stage


@dataclass(frozen=True)
class Contributor:
    unit: UnitTarget
    paths: tuple


def _contributors(base, targets):
    catalog = CatalogStore(base.root, base.identity)
    return {
        unit.unit_revision_id: Contributor(
            unit,
            tuple(
                path
                for path, _ in source_paths(
                    unit, catalog.processing(unit.processing_id), retained=False
                )
            ),
        )
        for unit in targets.units
    }


def _summary_owner(page, baseline, contributors, generated):
    matches = [
        revision
        for revision, item in contributors.items()
        if page == f"summaries/{item.unit.doc_name}.md"
    ]
    if len(matches) != 1 or generated.get(page) != baseline.files.get(page):
        return None
    owner = matches[0]
    metadata = frontmatter.parse((baseline.directory / page).read_text("utf-8"))
    paths = set(contributors[owner].paths)
    sources = metadata.get("sources", [])
    return (
        owner
        if (
            metadata.get("type") == "Summary"
            and metadata.get("full_text") in paths
            and isinstance(sources, list)
            and all(source in paths for source in sources)
        )
        else None
    )


def _page_contributors(page, baseline, targets, contributors):
    wanted = {
        revision
        for revision, item in contributors.items()
        if item.unit.source_id in targets.source_ids
    }
    if not wanted:
        return None
    old = {}
    accepted = {item.unit.unit_id for item in contributors.values()}
    for revision in targets.basis.get(page, ()):
        unit = targets.old_units[revision]
        if unit.source_id in targets.source_ids and unit.unit_id not in accepted:
            return None
        old.setdefault(unit.unit_id, set()).add(revision)
    # Resolve actual source associations before installing the compile binding,
    # whose page-original inventory deliberately retains the older evidence.
    reader = CompileSourceWindows(
        baseline.directory, contributors[next(iter(wanted))].unit.doc_name
    )
    associated = reader._originals(page)
    by_path = {path: revision for revision, item in contributors.items() for path in item.paths}
    if not associated or set(associated) - by_path.keys():
        return None
    for path in associated:
        revision = by_path[path]
        unit = contributors[revision].unit
        prior = old.get(unit.unit_id, set())
        if not prior or (unit.source_id not in targets.source_ids and prior != {revision}):
            return None
        wanted.add(revision)
    return tuple(sorted(wanted))


async def _reconcile(
    scope, baseline, inputs, page, revisions, contributors, targets, context, bundle
):
    base, execution = scope.knowledge, context.storage_execution
    revisions = tuple(
        sorted(
            revisions, key=lambda r: (contributors[r].unit.source_id not in targets.source_ids, r)
        )
    )
    first = contributors[revisions[0]].unit
    work = CompileCheckpointStore(base.root, base.identity).for_work(
        execution.model_budget_id,
        object_id={
            "operation": "selected-refresh",
            "page": page,
            "publication_id": base.publication_id,
            "catalog_generation": targets.generation,
            "sources": sorted(targets.source_ids),
            "pages": sorted(targets.page_paths) if targets.page_paths is not None else None,
        },
        conditions={"fingerprint": context.executor.fingerprint, "compiler": 3},
        execution_id=execution.execution_id,
    )
    generated = base.generated_baselines().get(page)
    manual_base = None
    if generated is not None and generated != baseline.files.get(page):
        with storage_stage(base.root, base.identity, read_only=True) as db:
            manual_base = read_bytes(db, generated).decode("utf-8")
    registry = FrozenLinkRegistry.capture(
        baseline.directory, list_existing_wiki_targets(baseline.directory)
    )
    readers = [
        CompileSourceWindows(
            baseline.directory,
            contributors[r].unit.doc_name,
            kb_dir=base.root,
            view_id=scope.view_id,
        )
        for r in revisions
    ]
    with (
        usage_context(
            source_id=first.source_id,
            source_revision_id=first.source_revision_id,
            unit_id=first.unit_id,
            unit_revision_id=first.unit_revision_id,
        ),
        task_stage(
            context,
            "refresh.reconcile.v1",
            root=base.root,
            source_revision_id=first.source_revision_id,
            unit_revision_id=first.unit_revision_id,
        ),
        compile_binding(work, view_id=scope.view_id, document=first.doc_name, inputs=inputs),
        compilation_attempt() as report,
    ):
        evidence = [window for reader in readers for window in _full_windows(reader)]
        await reconcile_page(
            baseline.directory,
            page,
            readers[0],
            evidence,
            context.snapshot.values()["effective"],
            bundle,
            link_registry=registry,
            generated_baseline=manual_base,
        )
        report.require_complete()
        work.record_outcome(quality=report.quality, unfinished=report.unfinished)
        context.check_stop()


async def prepare_selected_refresh(scope, targets, context, bundle):
    base, basis = scope.knowledge, dict(targets.basis)
    captured, generated = base.files(), base.generated_baselines()
    contributors = _contributors(base, targets)
    resolved, changed = set(), set()
    with materialize_baseline(base, ()) as baseline:
        with task_stage(context, "refresh.inputs.v1", root=base.root):
            inputs = materialize_inputs(base, baseline, None)
        for page in sorted(targets.pages):
            context.check_stop()
            if page.startswith("summaries/"):
                owner = _summary_owner(page, baseline, contributors, generated)
                if owner is not None:
                    basis[page] = (owner,)
                    resolved.add(page)
                elif targets.page_paths is not None:
                    raise ValueError(
                        "Selected summary has no authenticated completed current owner"
                    )
            elif page.startswith(("concepts/", "entities/")):
                if not targets.may_reconcile(page):
                    continue
                revisions = _page_contributors(page, baseline, targets, contributors)
                if revisions is None:
                    continue
                await _reconcile(
                    scope, baseline, inputs, page, revisions, contributors, targets, context, bundle
                )
                selected_sources = {r["source_id"] for r in targets.reasons[page]}
                replacements = {
                    item.unit.unit_id: revision for revision, item in contributors.items()
                }
                basis[page] = tuple(
                    sorted(
                        set(revisions)
                        | {
                            replacements[targets.old_units[r].unit_id]
                            if targets.old_units[r].source_id in selected_sources
                            else r
                            for r in basis.get(page, ())
                        }
                    )
                )
                resolved.add(page)
                changed.add(page)
        replacements = {item.unit.unit_id: revision for revision, item in contributors.items()}
        if (
            "index.md" in targets.pages
            and all(page == "index.md" or page in resolved for page in targets.reasons)
            and set(targets.source_ids) <= {item.unit.source_id for item in contributors.values()}
            and all(
                targets.old_units[r].source_id not in targets.source_ids
                or targets.old_units[r].unit_id in replacements
                for r in basis.get("index.md", ())
            )
        ):
            _refresh_index(baseline.directory)
            replacements = {item.unit.unit_id: revision for revision, item in contributors.items()}
            basis["index.md"] = tuple(
                sorted(
                    {
                        replacements[targets.old_units[r].unit_id]
                        if targets.old_units[r].source_id in targets.source_ids
                        else r
                        for r in basis.get("index.md", ())
                    }
                )
            )
            changed.add("index.md")
        changes = {
            page: (baseline.directory / page).read_bytes()
            for page in changed
            if hashlib.sha256((baseline.directory / page).read_bytes()).hexdigest()
            != captured.get(page)
        }
        for path in baseline.directory.rglob("*"):
            if path.is_file() and not path.name.endswith(".md"):
                relative = path.relative_to(baseline.directory).as_posix()
                if not relative.startswith("sources/") and relative not in captured:
                    changes[relative] = path.read_bytes()
        remaining = tuple(
            sorted(path for path, units in basis.items() if set(units) & targets.stale_revisions)
        )
        protected = tuple(
            sorted(
                page
                for page in changes
                if page in captured
                and page.endswith(".md")
                and generated.get(page) != captured[page]
            )
        )
        with task_stage(context, "refresh.persist.v1", root=base.root):
            prepared = KnowledgeStore(base.root, base.identity)._prepare_revision(
                changes,
                expected=base.publication_id,
                view_id=scope.view_id,
                unit_revisions=base.unit_revisions(),
                index_revisions=base.index_revisions(),
                base=base,
                page_inputs=basis,
                generated_paths=tuple(page for page in changes if page.endswith(".md")),
                refresh_generation=targets.generation,
                compile_outcome=CompileOutcome(quality_known=bool(changed)),
            )
        return prepared, protected, remaining
