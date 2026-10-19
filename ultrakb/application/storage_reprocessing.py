"""Review, retain and resume a processing request against managed originals."""

import asyncio
from dataclasses import replace
from pathlib import PurePosixPath

from ultrakb.converter import _sanitize_stem
from ultrakb.ingest_result import IngestResult
from ultrakb.locks import LockCancelled
from ultrakb.pending.policies import CURRENT_POLICY, discovery_hosts
from ultrakb.storage.catalog_store import CatalogConflict, CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.reprocessing_store import ReprocessingPlan, ReprocessingStore

from .execution import ExecutionContext
from .reprocessing import ReprocessingConflict, runtime_availability
from .storage_ingestion import confirm_import_usage, process_admitted
from .storage_processing_policy import current_processing_policy, saved_processing_policy
from .storage_source_retry import retained_input
from .task_stages import task_stage


def request_complete(scope, catalog, request, *, units=None):
    units = catalog.source_units(request.source_id) if units is None else units
    revision = catalog.revision(request.source_revision_id)
    main = next(file for file in revision.files if file.kind == "main")
    source_format = PurePosixPath(main.path).suffix.lower()
    if source_format in {".xlsx", ".xls"}:
        from ultrakb.storage.workbook_store import WorkbookStore

        inventory = WorkbookStore(scope.kb_dir, scope.knowledge.identity).read(
            request.source_revision_id, processing_key=request.request_id
        )
        if inventory is None or inventory.error:
            return False
        keys = {sheet.key for sheet in inventory.sheets}
        if not keys <= {unit.key for unit in units}:
            return False
    elif not units:
        return False
    active = set(scope.knowledge.unit_revisions())
    for unit in units:
        processing = catalog.processing(unit.processing_id)
        if (
            processing.reprocessing_request_id != request.request_id
            or (processing.content_state == "content" and unit.unit_revision_id not in active)
            or (
                processing.content_state in {"empty", "withdrawn"}
                and not catalog.unit_exclusion(unit.unit_revision_id)
            )
        ):
            return False
    return True


def _preview(scope, source_id):
    base = scope.knowledge
    if (
        KnowledgeStore(scope.kb_dir, base.identity).bind(view_id=scope.view_id).publication_id
        != base.publication_id
    ):
        raise ReprocessingConflict("Knowledge changed; refresh the reprocessing preview")
    catalog = CatalogStore(scope.kb_dir, base.identity)
    source = catalog.source(source_id)
    if source.removed:
        raise ReprocessingConflict("The source was withdrawn; historical evidence retained")
    revision = catalog.revision(source.revision_id)
    units = catalog.source_units(source_id)
    main = next(file for file in revision.files if file.kind == "main")
    source_format = PurePosixPath(main.path).suffix.lower().lstrip(".")
    doc_name = (
        units[0].doc_name
        if units
        else f"{_sanitize_stem(PurePosixPath(main.path).stem)}-{source_id[:8]}"
    )
    blockers, assets = [], []

    def availability(file):
        try:
            catalog.read_file(revision.revision_id, file.path)
        except (OSError, ValueError) as error:
            return {"available": False, "digest": file.digest, "reason": str(error)}
        return {"available": True, "digest": file.digest, "reason": None}

    original = availability(main)
    assets = [
        {"reference": file.path, **availability(file)}
        for file in revision.files
        if file.kind == "sidecar"
    ]
    if not original["available"] or any(not asset["available"] for asset in assets):
        blockers.append("Original or retained assets are unavailable")
    runtime = runtime_availability(scope.kb_dir, source_format)
    if not runtime["available"]:
        blockers.append(runtime["reason"])
    try:
        policy = current_processing_policy(scope, catalog, revision, doc_name)
    except (OSError, ValueError) as error:
        policy = {}
        blockers.append(str(error))
    requests = ReprocessingStore(scope.kb_dir, base.identity)
    previous = requests.latest(source.revision_id)
    resume = False
    if previous and previous.view_id == scope.view_id and previous.policy == policy:
        try:
            requests.check_publication(previous, base.publication_id)
        except CatalogConflict:
            pass
        else:
            current_ids = {unit.unit_id for unit in units}
            continuable = previous.units.keys() <= current_ids and all(
                previous.units.get(unit.unit_id) == unit.unit_revision_id
                or catalog.processing(unit.processing_id).reprocessing_request_id
                == previous.request_id
                for unit in units
            )
            resume = continuable and not request_complete(scope, catalog, previous)
    plan = (
        previous
        if resume
        else ReprocessingPlan(
            kb_id=base.identity.kb_id,
            generation=base.identity.generation,
            source_id=source_id,
            source_revision_id=source.revision_id,
            source_generation=source.generation,
            view_id=scope.view_id,
            publication_id=base.publication_id,
            units={unit.unit_id: unit.unit_revision_id for unit in units},
            policy=policy,
            previous_request_id=previous.request_id if previous else None,
        )
    )
    active_revisions = set(base.unit_revisions())
    active = {
        unit.unit_id: unit
        for unit in base.input_units()
        if unit.unit_revision_id in active_revisions
    }
    details = []
    for unit in units:
        processing = catalog.processing(unit.processing_id)
        previous = saved_processing_policy(catalog, unit)
        details.append(
            {
                "unit_id": unit.unit_id,
                "key": unit.key,
                "name": unit.doc_name,
                "target_revision_id": unit.unit_revision_id,
                "previous_policy": previous.values if previous else {},
                "next_policy": policy,
                "policy_changed": previous is None or previous.values != policy,
                "saved_processing": processing.decision.model_dump(mode="json"),
                "processing_error": None,
                "status": "completed" if unit == active.get(unit.unit_id) else "pending",
                "successful_revision_id": active[unit.unit_id].unit_revision_id
                if unit.unit_id in active
                else None,
                "proposal_id": None,
            }
        )
    return plan, {
        "version": plan.request_id,
        "resume": resume,
        "status": "blocked" if blockers else "ready",
        "source_id": source_id,
        "source_revision_id": source.revision_id,
        "name": source.label,
        "original": original,
        "assets": assets,
        "runtime": runtime,
        "units": details,
        "current_policy": policy,
        "range": "all_worksheets" if source_format in {"xls", "xlsx"} else "body",
        "publication_impact": {"view_id": scope.view_id, "superseded_proposals": []},
        "discovery": {
            "will_schedule": source_format in discovery_hosts(CURRENT_POLICY),
            "policy": CURRENT_POLICY,
        },
        "blockers": blockers,
    }


def preview_stored_reprocessing(scope, source_id):
    return _preview(scope, source_id)[1]


def reprocess_stored_source(scope, source_id, *, version, context=None, unit_id=None):
    root, identity = scope.kb_dir, scope.knowledge.identity
    if (
        KnowledgeStore(root, identity).bind(view_id=scope.view_id).publication_id
        != scope.publication_id
    ):
        raise ReprocessingConflict("Knowledge changed; refresh the reprocessing preview")
    requests = ReprocessingStore(root, identity)
    plan = requests.read(version)
    if plan is None:
        plan, preview = _preview(scope, source_id)
        if plan.request_id != version:
            raise ReprocessingConflict("Reprocessing preview changed; preview again")
        if preview["status"] != "ready":
            raise ValueError("Reprocessing blocked: " + "; ".join(preview["blockers"]))
        requests.retain(plan)
    if plan.source_id != source_id or plan.view_id != scope.view_id:
        raise ReprocessingConflict("Reprocessing request belongs to another source or view")
    try:
        requests.check_publication(plan, scope.publication_id)
    except CatalogConflict as error:
        raise ReprocessingConflict(str(error)) from error
    catalog = CatalogStore(root, identity)
    source = catalog.source(source_id)
    if source.removed:
        raise ReprocessingConflict("The source was withdrawn; historical evidence retained")
    if source.revision_id != plan.source_revision_id:
        raise ReprocessingConflict("Source changed; refresh the reprocessing preview")
    revision = catalog.revision(source.revision_id)
    doc_name = plan.policy.get("input", {}).get("doc_name")
    if current_processing_policy(scope, catalog, revision, doc_name) != plan.policy:
        raise ReprocessingConflict("Processing policy changed; refresh the preview")
    context = context or ExecutionContext()
    if context.storage_identity not in (None, identity):
        raise ValueError("Reprocessing belongs to another database generation")
    context.storage_identity = identity
    if context.storage_execution is None and context.resume_storage_execution_id is None:
        context.resume_storage_execution_id = requests.execution(version)
    result = None
    try:
        with context.begin(root) as bundle:
            if (
                current_processing_policy(scope, catalog, revision, doc_name, bundle=bundle)
                != plan.policy
            ):
                raise ReprocessingConflict("Captured processing policy differs from the preview")
            requests.bind_execution(version, context.storage_execution)
            from ultrakb.llm_usage import usage_context

            from .storage_discovery import schedule_discovery

            discovery = schedule_discovery(scope, revision, context, processing_key=plan.request_id)
            with (
                task_stage(context, "import.reprocess.v1", source_revision_id=revision.revision_id),
                retained_input(catalog, revision) as ready,
                usage_context(
                    root_import_id=discovery.root_import_id
                    if discovery
                    else context.storage_execution.model_budget_id
                ),
            ):
                result = process_admitted(
                    scope,
                    ready,
                    context,
                    bundle,
                    None,
                    catalog,
                    revision,
                    reprocessing=plan,
                    unit_id=unit_id,
                )
            context._storage_result = (
                "completed"
                if result.status in {"added", "skipped"}
                else "cancelled"
                if result.status == "stopped"
                else "failed"
            )
    except ReprocessingConflict:
        raise
    except (Exception, asyncio.CancelledError) as error:
        if result is not None:
            result = replace(result, unfinished=(*result.unfinished, "confirm execution cleanup"))
        else:
            result = IngestResult(
                source.source_key,
                "stopped"
                if isinstance(error, (LockCancelled, asyncio.CancelledError))
                else "blocked",
                (),
                source_id=source_id,
                source_revision_id=revision.revision_id,
                message=str(error),
                unfinished=("reprocessing",),
            )
    return confirm_import_usage(replace(result, source=source.source_key), context, root, identity)
