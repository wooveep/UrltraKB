"""A recovered object enters the ordinary managed-source and compiler use cases."""

import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

from ultrakb.import_text import (
    ImportTextRejected,
    preflight_import_text,
    rejection_result,
    retainable_without_text,
    validate_text_preflight,
)
from ultrakb.inputs import prepared_input
from ultrakb.llm_usage import usage_context
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.discovery_store import DiscoveryStore, object_identity

from .storage_ingestion import confirm_import_usage, process_admitted
from .task_stages import task_stage


def import_recovered(scope, work, context, bundle):
    store = DiscoveryStore(scope.kb_dir, scope.knowledge.identity)
    recovered = store.recovered(work.intent_id)
    if recovered is None:
        raise ValueError("A source import requires its accepted discovery checkpoint")
    parent, object_key, filename, payload = recovered
    if parent.view_id != scope.view_id:
        raise ValueError("Recovered input belongs to another frozen knowledge view")
    parent_revision = CatalogStore(scope.kb_dir, scope.knowledge.identity).revision(
        parent.source_revision_id
    )
    original = next(file for file in parent_revision.files if file.kind == "main")
    expected = object_identity(parent, object_key, payload)
    if work.intent_id != expected or parent.source_revision_id != work.source_revision_id:
        raise ValueError("Recovered import differs from its exact discovery checkpoint")
    with tempfile.TemporaryDirectory(prefix="ultrakb-recovered-input-") as temporary:
        path = Path(temporary) / (work.intent_id + PurePosixPath(filename).suffix)
        path.write_bytes(payload)
        with prepared_input(path) as ready:
            assessment = preflight_import_text(scope.kb_dir, ready, check_stop=context.check_stop)
            try:
                validate_text_preflight(ready, assessment)
            except ImportTextRejected as error:
                if not retainable_without_text(error.assessment):
                    return confirm_import_usage(
                        rejection_result(ready, error),
                        context,
                        scope.kb_dir,
                        scope.knowledge.identity,
                    )
            catalog = CatalogStore(scope.kb_dir, scope.knowledge.identity)
            with task_stage(context, "import.admit.v1", work_key=ready.digest):
                revision = catalog.admit(
                    ready.path, source_key="recovered:" + work.intent_id, label=filename
                )
                store.bind_source(work, revision, context.storage_execution)
                store.schedule(
                    revision,
                    root_import_id=work.root_import_id,
                    view_id=scope.view_id,
                    depth=parent.depth + 1,
                    ancestry=(*parent.ancestry, original.digest),
                    policy=parent.policy,
                )
            with usage_context(root_import_id=work.root_import_id):
                result = process_admitted(
                    scope,
                    ready,
                    context,
                    bundle,
                    None,
                    catalog,
                    revision,
                    text_assessment=assessment,
                )
            return confirm_import_usage(
                replace(result, source="recovered:" + work.intent_id),
                context,
                scope.kb_dir,
                scope.knowledge.identity,
            )
