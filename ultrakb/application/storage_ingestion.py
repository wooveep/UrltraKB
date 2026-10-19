"""Managed originals, retained normalization and the shared compiler import operation."""

import asyncio
from contextlib import ExitStack
from dataclasses import asdict, replace
from urllib.parse import quote

from ultrakb.compile_checkpoints import digest
from ultrakb.converter import _sanitize_stem
from ultrakb.execution_capacity import select_execution_mode
from ultrakb.import_text import (
    ImportTextRejected,
    preflight_import_text,
    rejection_result,
    validate_text_preflight,
)
from ultrakb.ingest_result import ImportUnitOutcome, IngestResult
from ultrakb.inputs import TEXT_SOURCE_EXTENSIONS, prepared_input, validate_source_root
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.locks import LockCancelled, atomic_write_json, atomic_write_text
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.usage_store import UsageStore
from ultrakb.text_formats import normalize_text_document

from .execution import ExecutionContext
from .recompilation import RecompileResult, recompile_document
from .task_stages import task_stage


def import_stored_document(
    scope,
    source,
    *,
    bundle,
    context,
    prepared,
    origin_url,
    source_root,
    on_event,
    download_remote_assets,
):
    root, identity = scope.kb_dir, scope.knowledge.identity
    context = context or ExecutionContext(on_event=on_event or (lambda event: None))
    if context.storage_identity not in (None, identity):
        raise ValueError("Import belongs to another database generation")
    context.storage_identity = identity
    if bundle is not None:
        from ultrakb.config import LlmCredentialBundle

        if context.snapshot is not None and context.snapshot.values()["credentials"] != asdict(
            bundle
        ):
            raise ValueError("Import credentials differ from the captured execution")
        if context.executor is not None and context.executor.bindings.credentials() != bundle:
            raise ValueError("Import credentials differ from the active executor")
        context.credential_bundle = LlmCredentialBundle(**asdict(bundle))
    source = source.expanduser().absolute()
    validate_source_root(source, source_root)
    result = None
    try:
        with context.begin(root) as bundle, ExitStack() as inputs:
            ready = (
                prepared if prepared is not None else inputs.enter_context(prepared_input(source))
            )
            context.check_stop()
            validate_source_root(source, source_root)
            if not ready.is_current():
                from ultrakb.inputs import InputChanged

                raise InputChanged("The input changed before admission")
            try:
                validate_text_preflight(
                    ready, preflight_import_text(root, ready, check_stop=context.check_stop)
                )
            except ImportTextRejected as error:
                result = rejection_result(ready, error)
            else:
                result = _import_body(
                    scope, ready, context, bundle, origin_url, download_remote_assets
                )
            context._storage_result = (
                "completed"
                if result.status in {"added", "skipped"}
                else "cancelled"
                if result.status == "stopped"
                else "failed"
            )
    except (Exception, asyncio.CancelledError) as error:
        context._storage_result = "failed"
        if result is not None and result.status == "added":
            result = replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        else:
            result = IngestResult(
                str(source),
                "stopped"
                if isinstance(error, (LockCancelled, asyncio.CancelledError))
                else "blocked",
                (),
                unfinished=("import",),
                message=str(error),
            )
    if context.storage_execution is not None:
        try:
            result = replace(
                result,
                model_usage=UsageStore(root, identity).receipt(
                    execution_id=context.storage_execution.execution_id
                ),
            )
        except Exception:
            result = replace(result, unfinished=(*result.unfinished, "confirm usage"))
    return result


def _import_body(scope, ready, context, bundle, origin_url, download_remote_assets):
    root, identity = scope.kb_dir, scope.knowledge.identity
    catalog = CatalogStore(root, identity)
    revision = None
    try:
        with task_stage(context, "import.admit.v1", work_key=ready.digest):
            revision = catalog.admit(
                ready.path,
                source_key=origin_url or str(ready.identity),
                sidecars={
                    "sidecars/" + quote(reference, safe=""): image.path
                    for reference, image in ready.images.items()
                    if image.path is not None
                },
            )
        return _process_admitted(
            scope, ready, context, bundle, download_remote_assets, catalog, revision
        )
    except (Exception, asyncio.CancelledError) as error:
        if revision is None:
            raise
        return IngestResult(
            str(ready.identity),
            "stopped" if isinstance(error, (LockCancelled, asyncio.CancelledError)) else "blocked",
            (),
            source_id=revision.source_id,
            source_revision_id=revision.revision_id,
            input_version=ready.digest,
            unfinished=("processing",),
            message=str(error),
        )


def _process_admitted(scope, ready, context, bundle, download_remote_assets, catalog, revision):
    root = scope.kb_dir
    target = catalog.unit_for_source(revision.source_id, key="body")
    if target is None or target.source_revision_id != revision.revision_id:
        with task_stage(context, "import.process.v1", source_revision_id=revision.revision_id):
            doc_name = (
                target.doc_name
                if target
                else f"{_sanitize_stem(ready.source.stem)}-{revision.source_id[:8]}"
            )
            with materialize_baseline(scope.knowledge, ()) as baseline:
                private = KnowledgeScope(
                    kb_dir=root,
                    view_id=scope.view_id,
                    wiki_dir=baseline.directory,
                    private_workspace=baseline.private,
                )
                if ready.source.suffix.lower() in TEXT_SOURCE_EXTENSIONS:
                    resource_policy = None
                    if ready.source.suffix.lower() in {".html", ".htm"}:
                        from ultrakb.remote_assets import resolve_resource_policy

                        resource_policy = resolve_resource_policy(root, download_remote_assets)
                    frozen = normalize_text_document(
                        ready, doc_name, baseline.directory, resource_policy=resource_policy
                    )
                    body = baseline.directory / f"sources/{doc_name}.md"
                    native = body.with_suffix(".content.json")
                    atomic_write_text(body, frozen.text)
                    atomic_write_json(native, frozen.model_dump(mode="json"))
                    decision = select_execution_mode(
                        root,
                        classify_markdown_tokens(frozen.tokens),
                        body,
                        doc_name,
                        scope=private,
                        bundle=bundle,
                    )
                    if decision.execution_mode == "segmented":
                        from ultrakb.block_package import freeze_content_blocks

                        frozen = freeze_content_blocks(frozen.model_dump(mode="json"))
                        atomic_write_json(native, frozen.model_dump(mode="json"))
                    source_kind = "text"
                    files = {path: baseline.directory / path for path in frozen.assets}
                else:
                    from .storage_processing import convert_native_input

                    decision, body, native, files = convert_native_input(
                        scope, ready, baseline, private, doc_name, context, bundle
                    )
                    files = {
                        name: path for name, path in files.items() if path not in {body, native}
                    }
                    source_kind = "pages"
                fingerprint = digest(
                    {"native": json_native(native), "decision": decision.model_dump(mode="json")}
                )
                processing = catalog.retain_processing(
                    revision.revision_id,
                    {
                        body.name: body,
                        native.name: native,
                        **files,
                    },
                    body_path=body.name,
                    source_path=native.name,
                    source_kind=source_kind,
                    fingerprint=fingerprint,
                    operation_id=digest([revision.revision_id, fingerprint]),
                    decision=decision,
                )
                target = catalog.plan_unit(processing.processing_id, key="body", doc_name=doc_name)
    reused = target.unit_revision_id in {
        unit.unit_revision_id for unit in scope.knowledge.input_units()
    }
    decision = catalog.processing(target.processing_id).decision
    result = (
        RecompileResult(
            "compiled",
            target.doc_name,
            version=scope.publication_id,
            knowledge_revision_id=scope.knowledge.publication.revision_id,
        )
        if reused
        else asyncio.run(
            recompile_document(
                root, revision.source_id, unit_id=target.unit_id, context=context, scope=scope
            )
        )
    )
    compiled = result.status == "compiled"
    return IngestResult(
        str(ready.identity),
        "skipped"
        if reused
        else "added"
        if compiled
        else "stopped"
        if result.status == "stopped"
        else "blocked",
        (),
        source_id=revision.source_id,
        source_revision_id=revision.revision_id,
        input_version=ready.digest,
        units=(
            ImportUnitOutcome(
                target.unit_id,
                "completed" if compiled else result.status,
                target.unit_revision_id,
                successful_revision_id=target.unit_revision_id if compiled else None,
                successful_source_revision_id=revision.revision_id if compiled else None,
                knowledge_revision_id=result.knowledge_revision_id if compiled else None,
                view_id=scope.view_id,
                doc_name=target.doc_name,
                name=ready.source.name,
                length_class=decision.length_class,
                execution_mode=decision.execution_mode,
                processing=decision.model_dump(mode="json"),
                unfinished=result.unfinished,
                message=result.message,
            ),
        ),
        unfinished=result.unfinished,
        message=result.message,
    )


def json_native(path):
    import json

    return json.loads(path.read_text("utf-8"))
