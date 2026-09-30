"""Preview policy changes without processing; execute only the reviewed frozen input."""

import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

from openkb.application.execution import ExecutionContext
from openkb.application.file_state import contained_paths
from openkb.application.recompilation import _version
from openkb.application.sources import source_view_id
from openkb.config import resolve_effective_config
from openkb.ingest_records import UnitRevision
from openkb.inputs import OFFICE_SOURCE_EXTENSIONS, PreparedImage, PreparedInput
from openkb.knowledge_scope import KnowledgeScope, live_scope, resolve_scope
from openkb.lifecycle import read_lifecycle
from openkb.locks import kb_ingest_lock, kb_read_lock
from openkb.normalization import normalization_fingerprint, read_processing
from openkb.pending.policies import CURRENT_POLICY, discovery_hosts
from openkb.source_catalog import (
    admit_source_revision,
    list_sources,
    read_admission,
    read_record,
    read_source,
    read_source_revision,
)
from openkb.state import HashRegistry
from openkb.unit_publication import list_source_units, read_unit_publication
from openkb.view_records import VersionAnnotation


class ReprocessingConflict(ValueError):
    """The previously reviewed processing plan no longer matches the current source."""


def _availability(kb_dir, relative, digest):
    if relative is None:
        return {
            "path": None,
            "available": False,
            "digest": None,
            "reason": "Not retained at admission",
        }
    path = kb_dir / relative
    contained_paths(kb_dir, [path])
    try:
        available = path.is_file() and HashRegistry.hash_file(path) == digest
    except OSError:
        available = False
    return {
        "path": relative,
        "available": available,
        "digest": digest,
        "reason": None if available else "Frozen input is missing or its digest changed",
    }


def runtime_availability(kb_dir, source_format):
    runtime = {
        "required": f".{source_format}" in OFFICE_SOURCE_EXTENSIONS,
        "available": True,
        "reason": None,
    }
    if runtime["required"]:
        from openkb.office.runtime import runtime_path, validate_runtime

        try:
            validate_runtime(runtime_path(kb_dir))
        except (OSError, ValueError) as exc:
            runtime.update(available=False, reason=str(exc))
    return runtime


def _seal_preview(root, value):
    value["version"] = hashlib.sha256(
        json.dumps([_version(root), value], sort_keys=True).encode()
    ).hexdigest()
    return value


def preview_reprocessing(
    kb_dir: Path, source_id: str, *, scope: KnowledgeScope | None = None
) -> dict:
    """Read availability and policy without conversion, worksheet parsing or model calls."""
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        source = next(
            (
                item
                for item in list_sources(root)
                if source_id in {item.source_id, item.legacy_hash}
            ),
            None,
        )
        admission = read_admission(root, source) if source else None
        if admission is None or admission.revision.original_kind == "legacy_snapshot":
            from openkb.application.legacy_reprocessing import preview_legacy_reprocessing

            return _seal_preview(
                root, preview_legacy_reprocessing(root, source_id, source, scope=scope)
            )
        source = admission.source
        source_id = source.source_id
        revision = admission.revision
        view_id = source_view_id(root, source)
        scope = resolve_scope(root, scope) if scope else live_scope(root, view_id)
        blockers = []
        if source.removed or scope.read_only or scope.view_id != view_id:
            blockers.append("Choose the current source in its live knowledge view")
        if revision.original_kind != "original":
            blockers.append(
                "Legacy normalized snapshot is not a retained original; reprocessing is unavailable"
            )
        original = _availability(root, revision.original, revision.digest)
        assets = [
            {
                "reference": asset.original_reference,
                **_availability(root, asset.artifact, asset.digest),
            }
            for asset in revision.assets
        ]
        if not original["available"] or any(
            item["digest"] and not item["available"] for item in assets
        ):
            blockers.append("Original or retained assets are missing or changed")
        current_policy = json.loads(
            normalization_fingerprint(
                root,
                scope=scope,
                source_revision=_preview_decoding(root, revision, original["available"]),
                doc_name=source.doc_name,
            )
        )
        runtime = runtime_availability(root, revision.source_format)
        if not runtime["available"]:
            blockers.append(runtime["reason"])
        units = []
        for unit in list_source_units(root, source_id):
            try:
                publication = read_unit_publication(root, unit.unit_id, view_id)
            except FileNotFoundError:
                publication = None
            target = read_record(root, "unit-revisions", unit.target_revision_id, UnitRevision)
            try:
                before = json.loads(target.processing_fingerprint)
            except ValueError:
                before = {"legacy": target.processing_fingerprint}
            after = dict(current_policy)
            if unit.key != "body":
                from openkb.workbooks.records import WORKBOOK_POLICY, XLS_POLICY

                after["sheet"] = {
                    "key": unit.key,
                    "policy": XLS_POLICY if revision.source_format == "xls" else WORKBOOK_POLICY,
                }
            try:
                saved_processing = read_processing(root, unit.target_revision_id)
                processing_error = None
            except (OSError, ValueError) as exc:
                saved_processing, processing_error = None, str(exc)
            units.append(
                {
                    "unit_id": unit.unit_id,
                    "key": unit.key,
                    "name": unit.name or source.name,
                    "target_revision_id": unit.target_revision_id,
                    "previous_policy": before,
                    "next_policy": after,
                    "policy_changed": before != after,
                    "saved_processing": saved_processing,
                    "processing_error": processing_error,
                    "status": publication.status if publication else "unpublished",
                    "successful_revision_id": publication.successful_revision_id
                    if publication
                    else None,
                    "proposal_id": publication.proposal_id if publication else None,
                }
            )
        from openkb.application.version_review import VersionReview
        from openkb.source_catalog import record_path

        review_path = record_path(root, "version-reviews", revision.source_revision_id)
        review = (
            read_record(root, "version-reviews", revision.source_revision_id, VersionReview)
            if review_path.exists()
            else None
        )
        if review and review.status in {"blocked", "ready", "cancelled"}:
            blockers.append("Resolve the version clarification before reprocessing this source")
        annotation = (
            read_record(root, "annotations", source.annotation_id, VersionAnnotation)
            if source.annotation_id
            else None
        )
        intent = admission.discovery_intent
        config, origins = resolve_effective_config(root)
        value = {
            "status": "blocked" if blockers else "ready",
            "source_id": source_id,
            "source_revision_id": revision.source_revision_id,
            "name": source.name,
            "original": original,
            "assets": assets,
            "runtime": runtime,
            "units": units,
            "current_policy": current_policy,
            "range": "all_worksheets" if revision.source_format in {"xls", "xlsx"} else "body",
            "range_note": "Read all worksheets again; retain failed units' old results"
            if revision.source_format in {"xls", "xlsx"}
            else "Process the complete frozen document",
            "version_impact": {
                "view_id": view_id,
                "metadata": annotation.metadata.model_dump(mode="json") if annotation else None,
                "review_status": review.status if review else None,
                "superseded_proposals": [
                    unit["proposal_id"] for unit in units if unit["proposal_id"]
                ],
                "note": "Retain version annotation; manual edits still require proposal acceptance",
            },
            "discovery": {
                "will_schedule": revision.source_format in discovery_hosts(CURRENT_POLICY),
                "previous_policy": intent.policy,
                "next_policy": CURRENT_POLICY,
                "previous_root_import_id": intent.root_import_id,
                "previous_status": intent.status,
                "budget": config["extraction_budget"],
                "budget_origin": origins["extraction_budget"],
                "note": "Create a separate extraction group; preserve existing jobs and sources",
            },
            "blockers": blockers,
        }
        return _seal_preview(root, value)


def _preview_decoding(kb_dir, revision, available):
    if available and revision.source_format in {"txt", "csv", "xml", "html", "htm"}:
        from openkb.text_encoding import inspect_text_encoding

        return revision.model_copy(
            update={
                "text_decoding": inspect_text_encoding(
                    (kb_dir / revision.original).read_bytes(),
                    source_format=revision.source_format,
                )
            }
        )
    return revision


def _frozen_input(kb_dir, source, revision):
    path = kb_dir / revision.original
    if not _availability(kb_dir, revision.original, revision.digest)["available"] or any(
        asset.digest and not _availability(kb_dir, asset.artifact, asset.digest)["available"]
        for asset in revision.assets
    ):
        raise ValueError("Retained original or assets are missing or changed")
    return PreparedInput(
        Path(f"{source.doc_name}.{revision.source_format}"),
        path,
        revision.digest,
        {
            asset.original_reference: PreparedImage(
                Path(unquote(asset.original_reference)),
                kb_dir / asset.artifact if asset.artifact else None,
                asset.digest,
            )
            for asset in revision.assets
        },
        path,
    )


def reprocess_source(
    kb_dir: Path,
    source_id: str,
    *,
    version: str,
    scope: KnowledgeScope | None = None,
    context: ExecutionContext | None = None,
):
    """CAS a new request or resume only the current request identified by the same token."""
    from dataclasses import replace

    root = kb_dir.resolve()
    context = context or ExecutionContext()
    from openkb.compilation_report import collect_compile_report

    with (
        read_lifecycle(root, cancelled=context.cancelled, on_wait=context.waiting),
        kb_ingest_lock(root / ".openkb", cancelled=context.cancelled, on_wait=context.waiting),
        context.begin(root) as credentials,
        collect_compile_report() as compilation,
    ):
        result = _execute_reprocessing(root, source_id, version, scope, context, credentials)
        return replace(
            result,
            quality=tuple(compilation.quality),
            unfinished=result.unfinished + tuple(compilation.unfinished),
        )


def _execute_reprocessing(root, source_id, version, scope, context, credentials):
    from openkb.application.ingestion import import_prepared_source

    existing = next(
        (item for item in list_sources(root) if source_id in {item.source_id, item.legacy_hash}),
        None,
    )
    admission = read_admission(root, existing) if existing else None
    frozen = admission.revision if admission else None
    if version and existing and frozen and frozen.reprocessing_request == version:
        selected = resolve_scope(root, scope, writable=True) if scope else None
        if existing.removed or selected and selected.view_id != source_view_id(root, existing):
            raise ReprocessingConflict("Source view changed; preview again before reprocessing")
        return import_prepared_source(
            root,
            _frozen_input(root, existing, frozen),
            admission=admission,
            bundle=credentials,
            context=context,
            on_event=context.on_event,
            scope=scope,
            retry_confirmed=True,
        )
    else:
        preview = preview_reprocessing(root, source_id, scope=scope)
        if not version or preview["version"] != version:
            raise ReprocessingConflict(
                "Reprocessing preview changed; preview again before executing"
            )
        if preview["status"] != "ready":
            raise ValueError("Reprocessing blocked: " + "; ".join(preview["blockers"]))
        if preview.get("legacy_hash"):
            from openkb.application.legacy_reprocessing import reprocess_legacy

            return reprocess_legacy(root, preview, context, credentials)
        source = read_source(root, preview["source_id"])
        revision = read_source_revision(root, source.target_revision_id)
        prepared = _frozen_input(root, source, revision)
        from openkb.application.ingestion import import_prepared_source

        admission = admit_source_revision(
            root,
            prepared,
            identity=source.identity,
            name=source.name,
            reprocess_from=revision.source_revision_id,
            reprocessing_request=version,
            reprocessing_policy=json.dumps(preview["current_policy"], sort_keys=True),
            check_stop=context.check_stop,
        )
        return import_prepared_source(
            root,
            prepared,
            admission=admission,
            bundle=credentials,
            context=context,
            on_event=context.on_event,
            scope=scope,
        )
