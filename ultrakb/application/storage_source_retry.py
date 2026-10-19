"""Resume admitted work from CAS, without reopening the original external path."""

import asyncio
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

from ultrakb.inputs import PreparedImage, PreparedInput
from ultrakb.storage.blobs import read_bytes
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.execution_store import ExecutionStore

from .execution import ExecutionContext
from .source_retry import RetryTargetChanged
from .storage_ingestion import confirm_import_usage, process_admitted


@contextmanager
def retained_input(catalog, revision):
    with tempfile.TemporaryDirectory(prefix="ultrakb-retained-input-") as temporary:
        directory, images = Path(temporary), {}
        with storage_stage(catalog.root, catalog.identity, read_only=True) as db:
            for index, file in enumerate(revision.files):
                path = directory / str(index) / Path(file.path).name
                path.parent.mkdir()
                path.write_bytes(read_bytes(db, file.digest))
                if file.kind == "main":
                    main, digest = path, file.digest
                else:
                    reference = unquote(file.path.removeprefix("sidecars/"))
                    images[reference] = PreparedImage(path, path, file.digest)
        ready = PreparedInput(main, main, digest, images, main)
        try:
            yield ready
        finally:
            ready.conversion_tasks.close()


def retry_stored_source(
    scope, source_id, *, unit_id, context, source_revision_id, unit_revision_id
):
    root, identity = scope.kb_dir, scope.knowledge.identity
    catalog = CatalogStore(root, identity)
    source = catalog.source(source_id)
    if source.removed:
        raise RetryTargetChanged("The source was withdrawn; historical evidence retained")
    if source_revision_id is not None and source_revision_id != source.revision_id:
        raise RetryTargetChanged("Source target changed; check the remaining work again")
    revision = catalog.revision(source.revision_id)
    units = catalog.source_units(source_id)
    if unit_id is not None:
        units = tuple(unit for unit in units if unit.unit_id == unit_id)
        if not units or units[0].source_revision_id != source.revision_id:
            raise RetryTargetChanged("Worksheet target changed; check the remaining work again")
    if unit_revision_id is not None and (
        len(units) != 1 or units[0].unit_revision_id != unit_revision_id
    ):
        raise RetryTargetChanged("Processing target changed; check the remaining work again")
    from ultrakb.storage.reprocessing_store import ReprocessingStore

    if request := ReprocessingStore(root, identity).latest(revision.revision_id):
        from .storage_reprocessing import reprocess_stored_source, request_complete

        if not request_complete(scope, catalog, request, units=units):
            return reprocess_stored_source(
                scope, source_id, version=request.request_id, context=context, unit_id=unit_id
            )
    context = context or ExecutionContext()
    if context.storage_identity not in (None, identity):
        raise ValueError("Source retry belongs to another database generation")
    context.storage_identity = identity
    if context.storage_execution is None and context.resume_storage_execution_id is None:
        context.resume_storage_execution_id = ExecutionStore(root, identity).source_execution(
            source.revision_id
        )
    result = None
    try:
        with context.begin(root) as bundle, retained_input(catalog, revision) as ready:
            result = process_admitted(
                scope, ready, context, bundle, None, catalog, revision, unit_id=unit_id
            )
            context._storage_result = (
                "completed"
                if result.status in {"added", "skipped"}
                else "cancelled"
                if result.status == "stopped"
                else "failed"
            )
    except (Exception, asyncio.CancelledError):
        if result is None:
            raise
        result = replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
    return confirm_import_usage(replace(result, source=source.source_key), context, root, identity)
