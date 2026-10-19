"""The reviewed content recipe travels with each immutable processing result."""

import json
from pathlib import PurePosixPath
from urllib.parse import unquote

from pydantic import Field

from ultrakb.config import resolve_effective_config
from ultrakb.normalization import normalization_policy
from ultrakb.source_records import Digest, Record
from ultrakb.storage.catalog_store import CatalogConflict

POLICY_PATH = "processing-policy.json"


class ProcessingPolicy(Record):
    version: int = Field(default=1, ge=1, le=1)
    values: dict
    request_id: Digest | None = None


def current_processing_policy(
    scope, catalog, revision, doc_name, *, bundle=None, resource_policy=None
):
    main = next(file for file in revision.files if file.kind == "main")
    source_format = PurePosixPath(main.path).suffix.lower().lstrip(".")
    decoding = None
    if source_format in {"txt", "csv", "xml", "html", "htm"}:
        from ultrakb.text_encoding import inspect_text_encoding

        decoding = inspect_text_encoding(
            catalog.read_original(revision.revision_id), source_format=source_format
        ).model_dump(mode="json")
    policy = json.loads(
        normalization_policy(
            scope.kb_dir,
            scope=scope,
            bundle=bundle,
            source_format=source_format,
            input_digest=main.digest,
            asset_digests={
                unquote(file.path.removeprefix("sidecars/")): file.digest
                for file in revision.files
                if file.kind == "sidecar"
            },
            text_decoding=decoding,
            doc_name=doc_name,
            resource_policy=resource_policy,
        )
    )
    # Capacity may change the selected execution mode while leaving extraction intact.
    policy["capacity"] = resolve_effective_config(scope.kb_dir)[0].get("model_capacity", {})
    return policy


def saved_processing_policy(catalog, target):
    processing = catalog.processing(target.processing_id)
    if not any(file.path == POLICY_PATH for file in processing.files):
        return None
    policy = ProcessingPolicy.model_validate_json(
        catalog.read_processed(target.processing_id, POLICY_PATH)
    )
    if policy.request_id != processing.reprocessing_request_id:
        raise ValueError("Processing policy differs from its reprocessing ownership")
    return policy


def needs_reprocessing(catalog, target, request):
    """Only a reviewed baseline or an already accepted result can advance this request."""
    if target is None:
        return True
    saved = saved_processing_policy(catalog, target)
    if saved and saved.request_id == request.request_id:
        if target.source_revision_id != request.source_revision_id:
            raise CatalogConflict("Reprocessing result belongs to another source revision")
        return False
    if request.units.get(target.unit_id) != target.unit_revision_id:
        raise CatalogConflict("Processing target changed after reprocessing preview")
    return True
