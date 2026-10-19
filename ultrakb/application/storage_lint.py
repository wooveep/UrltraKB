"""Fixed-publication lint with one atomic repair/report publication."""

import asyncio
import datetime
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace

from ultrakb.context.workers import run_blocking
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.lint import fix_broken_links, run_structural_lint
from ultrakb.locks import LockCancelled, atomic_write_text
from ultrakb.log import appended_log
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.compile_inputs import materialize_inputs
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict
from ultrakb.storage.lint_store import LintDetails, LintStore
from ultrakb.storage.wiki_projection import ProjectionConflict

from .execution import current_execution
from .maintenance import LintResult
from .publications import sync_publication
from .storage_execution import begin_storage_operation
from .task_stages import task_stage


def _version(scope):
    base = scope.knowledge
    return f"lint-v1:{base.identity.generation}:{base.publication_id}"


@contextmanager
def _workspace(scope):
    with materialize_baseline(scope.knowledge, ()) as baseline:
        # Maintenance preserves existing image names; it only repairs wikilinks.
        materialize_inputs(scope.knowledge, baseline, None, freeze_images=False)
        yield KnowledgeScope(
            scope.kb_dir,
            baseline.directory,
            scope.view_id,
            private_workspace=baseline.private,
        )


def preview_stored_lint(scope):
    with _workspace(scope) as private:
        return _version(scope), run_structural_lint(scope.kb_dir, scope=private)


def _report(private, result, *, unique):
    wiki = private.wiki_dir
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    relative = f"reports/lint_{timestamp}.md"
    counter = 1
    while unique and (wiki / relative).exists():
        relative = f"reports/lint_{timestamp}_{counter}.md"
        counter += 1
    content = f"# Lint Report — {timestamp}\n\n## Structural\n\n{result.structural_report}\n"
    if result.knowledge_report is not None:
        content += f"\n## Semantic\n\n{result.knowledge_report}\n"
    atomic_write_text(wiki / relative, content)
    log = wiki / "log.md"
    atomic_write_text(
        log,
        appended_log(
            log.read_text("utf-8") if log.exists() else None,
            "lint",
            f"report → {relative.removeprefix('reports/')}",
        ),
    )
    return relative


def _publish(scope, candidate, context):
    base, prepared, details = scope.knowledge, candidate.prepared, candidate.details
    store = KnowledgeStore(base.root, base.identity)
    observed = current_execution() is context
    publication, unconfirmed = None, ()
    try:
        with (
            task_stage(context, "knowledge.publish.v1", root=base.root)
            if observed
            else nullcontext()
        ):
            try:
                publication = store.publish(
                    prepared,
                    check_cancelled=context.check_stop,
                    execution_id=context.storage_execution.execution_id if observed else None,
                )
            except Exception:
                publication = store.committed_publication(prepared)
                if publication is None:
                    raise
            context._storage_result = "completed"
    except Exception:
        if publication is None:
            raise
        unconfirmed = ("confirm publication observation",)
    path = str(base.root / "wiki" / details.report_path)
    result = LintResult(
        "completed",
        structural_report=details.structural_report,
        knowledge_report=details.knowledge_report,
        files_changed=details.files_changed,
        ghosts_removed=details.ghosts_removed,
        report_path=path,
        resources=(path,),
        changes=tuple(f"saved: wiki/{path}" for path in details.changes),
        quality=details.quality,
        publication_id=publication.publication_id,
        projection_pending=True,
        unfinished=(
            *(("semantic_lint",) if details.quality else ()),
            "synchronize wiki",
            *unconfirmed,
        ),
    )
    try:
        with task_stage(context, "wiki.sync.v1", root=base.root) if observed else nullcontext():
            projection = sync_publication(base.root, base.identity, prepared)
    except Exception:
        return result
    if projection.status in {"synced", "obsolete"}:
        return replace(
            result,
            projection_pending=False,
            unfinished=(*(("semantic_lint",) if details.quality else ()), *unconfirmed),
        )
    return result


async def check_stored_knowledge(scope, options, context, *, bundle=None):
    base = scope.knowledge
    if context.storage_identity not in (None, base.identity):
        raise ValueError("Lint belongs to another database generation")
    context.storage_identity = base.identity
    if bundle is not None:
        from ultrakb.config import LlmCredentialBundle

        if context.snapshot is not None and context.snapshot.values()["credentials"] != asdict(
            bundle
        ):
            raise ValueError("Lint credentials differ from the captured execution")
        if context.executor is not None and context.executor.bindings.credentials() != bundle:
            raise ValueError("Lint credentials differ from the active executor")
        context.credential_bundle = LlmCredentialBundle(**asdict(bundle))
    if options.version is not None and options.version != _version(scope):
        return LintResult("conflict")
    store = LintStore(base.root, base.identity)
    settings = {"fix": options.fix, "semantic": options.semantic, "unique": options.unique_report}
    execution = context.storage_execution
    if execution is None and context.resume_storage_execution_id is not None:
        from ultrakb.storage.execution_store import ExecutionStore

        execution = ExecutionStore(base.root, base.identity).execution(
            context.resume_storage_execution_id
        )
    if execution is not None:
        previous = store.find(base, execution.model_budget_id, **settings)
        if previous is not None and KnowledgeStore(base.root, base.identity).committed_publication(
            previous.prepared
        ):
            return _publish(scope, previous, context)
    if KnowledgeStore(base.root, base.identity).bind(view_id=scope.view_id).publication_id != (
        base.publication_id
    ):
        return LintResult("conflict")
    result = None

    async def execute(active_bundle):
        nonlocal result
        previous = store.find(base, context.storage_execution.model_budget_id, **settings)
        if previous is not None and previous.sync_ready:
            result = _publish(scope, previous, context)
            return
        with _workspace(scope) as private:
            wiki = private.wiki_dir
            before = {
                path: (wiki / path).read_bytes() for path in base.files() if path.endswith(".md")
            }
            proposed = LintResult("completed")
            if options.fix:
                with task_stage(context, "knowledge.links.repair.v1", root=base.root):
                    files, ghosts = fix_broken_links(wiki)
                    proposed = replace(proposed, files_changed=files, ghosts_removed=ghosts)
                context.on_event({"stage": "links_repaired", "files": files, "ghosts": ghosts})
            with task_stage(context, "knowledge.lint.structural.v1", root=base.root):
                proposed = replace(
                    proposed, structural_report=run_structural_lint(base.root, scope=private)
                )
            context.on_event({"stage": "structural_report", "report": proposed.structural_report})
            if options.semantic:
                from .storage_lint_semantic import semantic_report

                report = await semantic_report(scope, private, options, context, active_bundle)
                proposed = replace(
                    proposed,
                    knowledge_report=report.text,
                    quality=tuple(report.quality),
                    unfinished=("semantic_lint",) if report.quality else (),
                )
                context.on_event({"stage": "semantic_report", "report": report.text})
            with task_stage(context, "knowledge.lint.report.v1", root=base.root):
                relative = _report(private, proposed, unique=options.unique_report)
                changes = {
                    path: (wiki / path).read_bytes()
                    for path in {*before, relative, "log.md"}
                    if (wiki / path).read_bytes() != before.get(path)
                }
                basis = {
                    relative: tuple(unit.unit_revision_id for unit in base.input_units()),
                }
                prepared = KnowledgeStore(base.root, base.identity).prepare_changes(
                    base, changes, page_inputs=basis
                )
                candidate = store.save(
                    base,
                    prepared,
                    LintDetails(
                        structural_report=proposed.structural_report,
                        knowledge_report=proposed.knowledge_report,
                        report_path=relative,
                        files_changed=proposed.files_changed,
                        ghosts_removed=proposed.ghosts_removed,
                        changes=tuple(sorted(changes)),
                        quality=proposed.quality,
                    ),
                    context.storage_execution,
                    **settings,
                )
            result = _publish(scope, candidate, context)

    def worker(active_bundle):
        asyncio.run(execute(active_bundle))

    try:
        if options.semantic:
            with context.begin(base.root) as captured:
                await run_blocking(lambda: worker(bundle if bundle is not None else captured))
        else:
            with begin_storage_operation(base.root, context, "maintain-knowledge"):
                await run_blocking(lambda: worker(None))
    except (Exception, asyncio.CancelledError) as error:
        if result is not None:
            return replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        if isinstance(error, (LockCancelled, asyncio.CancelledError)):
            raise
        return LintResult(
            "conflict"
            if isinstance(error, (PublicationConflict, ProjectionConflict))
            else "blocked"
            if isinstance(error, RecoveryRequired)
            else "failed",
            error_type=type(error).__name__,
            unfinished=("lint",),
        )
    return result
