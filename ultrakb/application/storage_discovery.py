"""Run the existing bounded Office recognizers against one frozen main-store original."""

import hashlib
from pathlib import PurePosixPath
from uuid import uuid4

from ultrakb.locks import LockCancelled
from ultrakb.pending.budget import BudgetWait, DiscoveryMeter
from ultrakb.pending.cfb import CompoundObjects, ContainerRebuildRequired
from ultrakb.pending.formats import recognize
from ultrakb.pending.ooxml import OOXMLObjects
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.discovery_store import DiscoveryStore
from ultrakb.storage.pending_store import PendingStore

from .storage_source_retry import retained_input
from .task_stages import task_stage


def schedule_discovery(scope, revision, context, **options):
    configured = context.snapshot.values()["effective"].get("extraction_budget")
    return DiscoveryStore(scope.kb_dir, scope.knowledge.identity).schedule(
        revision,
        root_import_id=uuid4().hex,
        view_id=scope.view_id,
        budget=configured,
        budget_origin="kb" if configured is not None else "default",
        **options,
    )


def discover_stored(scope, work, context):
    store = DiscoveryStore(scope.kb_dir, scope.knowledge.identity)
    value, cursor = store.input(work.intent_id)
    if value.view_id != scope.view_id:
        raise ValueError("Discovery belongs to another frozen knowledge view")
    catalog = CatalogStore(scope.kb_dir, scope.knowledge.identity)
    revision = catalog.revision(value.source_revision_id)
    original = next(file for file in revision.files if file.kind == "main")
    source_format = PurePosixPath(original.path).suffix.lower().lstrip(".")
    provider = CompoundObjects if source_format in {"doc", "xls", "ppt"} else OOXMLObjects
    with retained_input(catalog, revision) as ready, provider(ready.path) as package:
        meter = DiscoveryMeter(store.group(work.root_import_id), context.check_stop)
        try:
            with task_stage(
                context,
                "discovery.scan.v1",
                root=scope.kb_dir,
                source_revision_id=revision.revision_id,
            ):
                objects = package.scan(
                    meter, legacy=value.policy == "docx-embedded-package-v1", policy=value.policy
                )
        finally:
            store.record_meter(work, meter)
        for ordinal in range(cursor, len(objects)):
            context.check_stop()
            candidate = objects[ordinal]
            saved = store.checkpoint(work.intent_id, ordinal)
            if saved:
                if saved[1] != candidate.key:
                    raise ValueError("Frozen object inventory differs from its accepted checkpoint")
                continue
            group = store.group(work.root_import_id)
            if group.cancelled:
                raise LockCancelled("The import group was cancelled")
            meter = DiscoveryMeter(group, context.check_stop)
            payload, extension = b"", None
            outcome, diagnostic = candidate.outcome or "unknown", candidate.diagnostic
            with task_stage(
                context,
                "discovery.object.v1",
                root=scope.kb_dir,
                source_revision_id=revision.revision_id,
                work_key="object:" + hashlib.sha256(candidate.key.encode()).hexdigest(),
                ordinal=ordinal,
            ):
                try:
                    try:
                        meter.check()
                        if not diagnostic:
                            if value.depth + 1 > group.budget.max_depth:
                                raise BudgetWait("Discovery depth budget exhausted")
                            if group.sources >= group.budget.max_sources:
                                raise BudgetWait("Source count budget exhausted")
                            if (
                                candidate.size > group.budget.max_object_bytes
                                or group.object_bytes + candidate.size
                                > group.budget.max_total_bytes
                            ):
                                raise BudgetWait("Object byte budget exhausted")
                            payload, filename = package.read(candidate, meter)
                            payload, extension, outcome, diagnostic = recognize(
                                payload, filename, meter
                            )
                            if value.policy == "docx-embedded-package-v1" and extension != "docx":
                                extension, outcome, diagnostic = (
                                    None,
                                    "private_object",
                                    "Not supported by the retained DOCX discovery policy",
                                )
                            meter.check()
                    except (BudgetWait, LockCancelled):
                        raise
                    except Exception as error:
                        extension = None
                        outcome = (
                            "requires_container_rebuild"
                            if isinstance(error, ContainerRebuildRequired)
                            else "corrupt_object"
                        )
                        diagnostic = (
                            f"Object could not be recovered: {type(error).__name__}: {error}"
                        )
                finally:
                    store.record_meter(work, meter)
                if hashlib.sha256(payload).hexdigest() in (*value.ancestry, original.digest):
                    extension, outcome, diagnostic = (
                        None,
                        "cycle",
                        "Repeated content on this execution path; recursive cycle stopped",
                    )
                store.accept(
                    work,
                    value,
                    ordinal,
                    object_key=candidate.key,
                    payload=payload if extension and not diagnostic else None,
                    filename=(
                        PurePosixPath(candidate.key).name
                        if extension in {"caj", "kdh"}
                        else PurePosixPath(candidate.key).stem + "." + extension
                    )
                    if extension and not diagnostic
                    else None,
                    outcome=outcome,
                    diagnostic=diagnostic,
                )
    PendingStore(scope.kb_dir, scope.knowledge.identity).finish(work, status="completed")
