"""Rebuild a private full Markdown view using the existing compiler and durable work."""

import hashlib
import shutil
from dataclasses import replace

from ultrakb.compilation_report import compilation_attempt
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.llm_usage import usage_context
from ultrakb.locks import atomic_write_text
from ultrakb.schema import AGENTS_MD, INDEX_SEED
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.compile_binding import compile_binding
from ultrakb.storage.compile_inputs import materialize_inputs
from ultrakb.storage.compile_outcomes import CompileOutcome
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.index_store import IndexStore
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.knowledge_validity import publication_validity

from .task_stages import task_stage


def _files(directory):
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


async def prepare_refresh(scope, targets, context, bundle):
    base = scope.knowledge
    captured = base.files()
    pending = publication_validity(base)["needs_refresh"]
    active = tuple(unit.unit_revision_id for unit in targets.units)
    quality = []
    with materialize_baseline(base, ()) as initial:
        with task_stage(context, "refresh.inputs.v1", root=base.root):
            for section in ("sources", "summaries", "concepts", "entities"):
                shutil.rmtree(initial.directory / section, ignore_errors=True)
                (initial.directory / section).mkdir(parents=True)
            if not (initial.directory / "AGENTS.md").exists():
                atomic_write_text(initial.directory / "AGENTS.md", AGENTS_MD)
            atomic_write_text(initial.directory / "index.md", INDEX_SEED)
            for path in pending:
                if path.startswith(("reports/", "explorations/")):
                    (initial.directory / path).unlink(missing_ok=True)
            baseline = replace(
                initial,
                files={
                    path: digest
                    for path, digest in captured.items()
                    if not path.endswith(".md") or (initial.directory / path).is_file()
                },
            )
            basis = {
                path: units
                for path, units in base.page_input_revisions().items()
                if (initial.directory / path).is_file()
                and not path.startswith(("sources/", "summaries/", "concepts/", "entities/"))
                and path != "index.md"
            }
            if not targets.units:
                materialize_inputs(base, baseline, None, page_basis=basis, active_units=())
        for target in targets.units:
            before = _files(baseline.directory)
            with usage_context(
                source_id=target.source_id,
                source_revision_id=target.source_revision_id,
                unit_id=target.unit_id,
                unit_revision_id=target.unit_revision_id,
            ):
                inputs = materialize_inputs(
                    base, baseline, target, page_basis=basis, active_units=active
                )
                checkpoints, report = await _compile(
                    scope, baseline, inputs, target, targets.generation, context, bundle
                )
                outcome = checkpoints.record_outcome(
                    quality=report.quality, unfinished=report.unfinished
                )
                quality.extend(outcome.quality)
            after = _files(baseline.directory)
            for path, digest in after.items():
                if not path.endswith(".md"):
                    continue
                if path in inputs.identities:
                    basis[path] = (inputs.identities[path]["processing_revision_id"],)
                elif digest != before.get(path):
                    basis[path] = tuple(
                        dict.fromkeys((*basis.get(path, ()), target.unit_revision_id))
                    )
            basis = {path: units for path, units in basis.items() if path in after}
            baseline = replace(baseline, files={**baseline.files, **after})
        context.check_stop()
        changes = {}
        after = _files(baseline.directory)
        for path, digest in after.items():
            if digest != captured.get(path):
                changes[path] = (baseline.directory / path).read_bytes()
        for path in captured:
            if path not in after and (path.endswith(".md") or path.startswith("sources/")):
                changes[path] = None
        basis["index.md"] = active
        basis.setdefault("AGENTS.md", ())
        generated = base.generated_baselines()
        protected = tuple(
            sorted(
                path
                for path in changes
                if (
                    path.startswith(
                        ("summaries/", "concepts/", "entities/", "reports/", "explorations/")
                    )
                    or path in {"AGENTS.md", "index.md"}
                )
                and path in captured
                and generated.get(path) != captured[path]
            )
        )
        with task_stage(context, "refresh.persist.v1", root=base.root):
            prepared = KnowledgeStore(base.root, base.identity)._prepare_revision(
                changes,
                expected=base.publication_id,
                view_id=scope.view_id,
                unit_revisions=active,
                index_revisions=tuple(
                    index
                    for index in base.index_revisions()
                    if IndexStore(base.root, base.identity).revision(index).unit_revision_id
                    in active
                ),
                page_inputs=basis,
                base=base,
                generated_paths=tuple(
                    path
                    for path in after
                    if path.endswith(".md")
                    and (
                        path.startswith(("summaries/", "concepts/", "entities/"))
                        or path == "index.md"
                        or path == "AGENTS.md"
                        and path not in captured
                    )
                ),
                refresh_generation=targets.generation,
                compile_outcome=CompileOutcome(
                    quality_known=bool(targets.units), quality=tuple(dict.fromkeys(quality))
                ),
            )
        return prepared, protected


async def _compile(scope, baseline, inputs, target, generation, context, bundle):
    from ultrakb.agent.compiler import compile_long_doc, compile_short_doc

    base = scope.knowledge
    config = context.snapshot.values()["effective"]
    processing = CatalogStore(base.root, base.identity).processing(target.processing_id)
    execution = context.storage_execution
    checkpoints = CompileCheckpointStore(base.root, base.identity).for_work(
        execution.model_budget_id,
        object_id={
            "operation": "refresh",
            "unit_revision_id": target.unit_revision_id,
            "view_id": scope.view_id,
            "publication_id": base.publication_id,
            "catalog_generation": generation,
        },
        conditions={"fingerprint": context.executor.fingerprint, "compiler": 3},
        execution_id=execution.execution_id,
    )
    private = KnowledgeScope(
        base.root, baseline.directory, scope.view_id, private_workspace=baseline.private
    )
    with (
        task_stage(
            context,
            "refresh.generate.v1",
            root=base.root,
            source_revision_id=target.source_revision_id,
            unit_revision_id=target.unit_revision_id,
        ),
        compile_binding(
            checkpoints, view_id=scope.view_id, document=target.doc_name, inputs=inputs
        ),
        compilation_attempt() as report,
    ):
        options = {
            "max_concurrency": config.get("compile_concurrency", 4),
            "bundle": bundle,
            "scope": private,
        }
        if processing.decision.execution_mode == "segmented":
            indexes = IndexStore(base.root, base.identity)
            index = next(
                index
                for index in base.index_revisions()
                if indexes.revision(index).unit_revision_id == target.unit_revision_id
            )
            document = indexes.document(index)
            summary = baseline.directory / f"summaries/{target.doc_name}.md"
            atomic_write_text(summary, base.read(f"summaries/{target.doc_name}.md").decode())
            await compile_long_doc(
                target.doc_name,
                summary,
                document["doc_id"],
                base.root,
                config["model"],
                navigation=document["structure"],
                **options,
            )
        else:
            await compile_short_doc(
                target.doc_name,
                baseline.directory / f"sources/{target.doc_name}.md",
                base.root,
                config["model"],
                **options,
            )
        report.require_complete()
        context.check_stop()
    return checkpoints, report
