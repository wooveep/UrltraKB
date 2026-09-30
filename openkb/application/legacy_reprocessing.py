"""Explicitly admit a verified legacy original while retaining its old mixed knowledge."""

from dataclasses import dataclass
from pathlib import Path

from openkb.file_state import contained_paths
from openkb.inputs import FROZEN_SOURCE_EXTENSIONS, local_image_inputs, prepared_input
from openkb.knowledge_scope import live_scope, resolve_scope
from openkb.normalization import normalization_fingerprint
from openkb.source_records import FrozenAsset, TextDecoding
from openkb.state import HashRegistry


@dataclass(frozen=True)
class LegacyPolicyInput:
    """Only measured input facts needed to preview a not-yet-admitted original."""

    digest: str
    source_format: str
    assets: tuple[FrozenAsset, ...]
    text_decoding: TextDecoding | None = None


def _original(kb_dir, identity, meta):
    candidates = [meta.get("raw_path"), meta.get("path"), f"raw/{meta.get('name', '')}"]
    for relative in candidates:
        if not isinstance(relative, str) or not relative:
            continue
        path = (kb_dir / relative).resolve()
        try:
            contained_paths(kb_dir, [path])
            if (
                path.is_relative_to(kb_dir / "wiki")
                or path.is_relative_to(kb_dir / ".openkb")
                or not path.is_file()
                or path.suffix.lower() not in FROZEN_SOURCE_EXTENSIONS
            ):
                continue
            if HashRegistry.hash_file(path) == identity:
                return path
        except (OSError, ValueError):
            continue
    return None


def preview_legacy_reprocessing(kb_dir, identifier, source, *, scope=None):
    import json
    import uuid

    from openkb.application.reprocessing import runtime_availability
    from openkb.config import resolve_effective_config
    from openkb.pending.policies import CURRENT_POLICY, discovery_hosts

    identity = source.legacy_hash if source else identifier
    meta = HashRegistry(kb_dir / ".openkb/hashes.json").get(identity)
    if not meta:
        raise ValueError("Source is unavailable for reprocessing")
    if scope:
        scope = resolve_scope(kb_dir, scope)
    blockers = []
    if source and source.removed or scope and (scope.read_only or scope.view_id != "legacy"):
        blockers.append("Choose the current legacy source in the legacy view")
    original = _original(kb_dir, identity, meta)
    if original is None:
        blockers.append("Legacy original is missing or its registered digest cannot be verified")
    source_format = (original or Path(meta.get("name") or "")).suffix[1:].lower()
    if f".{source_format}" not in FROZEN_SOURCE_EXTENSIONS:
        blockers.append("Original format is unsupported by the current processor")
    doc_name = meta.get("doc_name") or Path(meta.get("name") or "").stem
    from pydantic import TypeAdapter

    from openkb.source_records import DocName

    TypeAdapter(DocName).validate_python(doc_name)
    extension = "json" if meta.get("type") in {"long_pdf", "pageindex_cloud"} else "md"
    normalized = kb_dir / "wiki/sources" / f"{doc_name}.{extension}"
    contained_paths(kb_dir, [normalized])
    if not normalized.is_file():
        blockers.append("Legacy normalized body is missing; its history cannot be captured")
    assets, policy_assets = [], []
    if original:
        for reference, path in local_image_inputs(original).items():
            digest = HashRegistry.hash_file(path) if path.is_file() else None
            assets.append(
                {
                    "reference": reference,
                    "path": str(path),
                    "digest": digest,
                    "available": digest is not None,
                    "reason": None if digest else "Missing at preview",
                }
            )
            policy_assets.append(
                FrozenAsset(
                    original_reference=reference,
                    digest=digest,
                    artifact=f".openkb/artifacts/{digest}/content" if digest else None,
                )
            )
    decoding = None
    if original and source_format in {"txt", "csv", "xml", "html", "htm"}:
        from openkb.text_encoding import inspect_text_encoding

        decoding = inspect_text_encoding(original.read_bytes(), source_format=source_format)
    current = json.loads(
        normalization_fingerprint(
            kb_dir,
            scope=live_scope(kb_dir, uuid.uuid4().hex),
            source_revision=LegacyPolicyInput(
                identity, source_format, tuple(policy_assets), decoding
            ),
            doc_name=doc_name,
        )
    )
    runtime = runtime_availability(kb_dir, source_format)
    if not runtime["available"]:
        blockers.append(runtime["reason"])
    config, origins = resolve_effective_config(kb_dir)
    return {
        "legacy_hash": identity,
        "status": "blocked" if blockers else "ready",
        "source_id": source.source_id if source else identity,
        "source_revision_id": source.target_revision_id if source else None,
        "name": meta.get("name") or doc_name,
        "original": {
            "path": original.relative_to(kb_dir).as_posix() if original else None,
            "available": original is not None,
            "digest": identity,
            "kind": "verified_legacy_original",
        },
        "assets": assets,
        "runtime": runtime,
        "current_policy": current,
        "units": [
            {
                "unit_id": None,
                "key": "body",
                "name": doc_name,
                "target_revision_id": None,
                "previous_policy": {"legacy": "unknown"},
                "next_policy": current,
                "policy_changed": True,
                "saved_processing": None,
            }
        ],
        "range": "all_worksheets" if source_format in {"xls", "xlsx"} else "body",
        "range_note": "Parse the original explicitly; retain legacy mixed knowledge as history",
        "version_impact": {
            "view_id": "legacy",
            "metadata": None,
            "review_status": None,
            "note": "Keep legacy knowledge separate; clarify version before compilation",
        },
        "discovery": {
            "will_schedule": source_format in discovery_hosts(CURRENT_POLICY),
            "previous_policy": "unknown",
            "next_policy": CURRENT_POLICY,
            "previous_root_import_id": None,
            "previous_status": "unknown",
            "budget": config["extraction_budget"],
            "budget_origin": origins["extraction_budget"],
        },
        "blockers": blockers,
    }


def reprocess_legacy(kb_dir, preview, context, credentials):
    """Caller owns the lease and checked the preview within the active configuration."""
    import json

    from openkb.application.ingestion import import_prepared_source
    from openkb.application.views import bind_source_view
    from openkb.legacy_sources import admit_legacy_snapshot
    from openkb.source_catalog import (
        Admission,
        admit_source_revision,
        read_record,
        read_source_revision,
    )
    from openkb.source_records import DiscoveryIntent
    from openkb.view_records import SourceMetadata

    identity = preview["legacy_hash"]
    meta = HashRegistry(kb_dir / ".openkb/hashes.json").get(identity)
    with prepared_input(kb_dir / preview["original"]["path"]) as prepared:
        if prepared.digest != identity:
            raise ValueError("Legacy original changed after preview")
        if {key: image.digest for key, image in prepared.images.items()} != {
            item["reference"]: item["digest"] for item in preview["assets"]
        }:
            from openkb.application.reprocessing import ReprocessingConflict

            raise ReprocessingConflict("Legacy assets changed after preview; preview again")
        source = admit_legacy_snapshot(kb_dir, identity, meta)
        frozen = read_source_revision(kb_dir, source.target_revision_id)
        intent = read_record(
            kb_dir, "discovery-intents", frozen.discovery_intent_id, DiscoveryIntent
        )
        if source.annotation_id is None:
            source = bind_source_view(
                kb_dir,
                Admission(source, frozen, intent),
                SourceMetadata(),
                scope=live_scope(kb_dir, "legacy"),
            )[0].source
        admission = admit_source_revision(
            kb_dir,
            prepared,
            identity=source.identity,
            name=source.name,
            reprocessing_request=preview["version"],
            reprocessing_policy=json.dumps(preview["current_policy"], sort_keys=True),
            check_stop=context.check_stop,
        )
        return import_prepared_source(
            kb_dir,
            prepared,
            admission=admission,
            context=context,
            bundle=credentials,
            on_event=context.on_event,
        )
