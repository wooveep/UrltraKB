"""Admit immutable input separately from subsequent knowledge publication."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, TypeVar

from pydantic import TypeAdapter

from openkb.catalog_schema import CatalogSchema, catalog_schema_path
from openkb.converter import _portable_path, _sanitize_stem
from openkb.file_state import contained_paths
from openkb.inputs import PreparedInput
from openkb.lifecycle import current_generation
from openkb.locks import atomic_write_json, kb_ingest_lock, kb_read_lock
from openkb.mutation import _copy_file_atomic, mutation_scope
from openkb.source_records import (
    DiscoveryIntent,
    FrozenAsset,
    Record,
    RecordId,
    Source,
    SourceRevision,
)
from openkb.state import HashRegistry

R = TypeVar("R", bound=Record)


def occupied_document_names(kb_dir: Path) -> set[str]:
    """Sources and worksheet units share the same knowledge-file namespace."""
    from openkb.ingest_records import ImportUnit

    legacy = HashRegistry(kb_dir / ".openkb/hashes.json").all_entries()
    return (
        {source.doc_name for source in list_sources(kb_dir)}
        | {
            read_record(kb_dir, "units", path.stem, ImportUnit).doc_name
            for path in (kb_dir / ".openkb/catalog/units").glob("*.json")
        }
        | {meta.get("doc_name") or Path(meta.get("name", "")).stem for meta in legacy.values()}
    )


def record_path(kb_dir: Path, collection: str, identity: str) -> Path:
    TypeAdapter(RecordId).validate_python(identity)
    if collection not in {
        "workbooks",
        "execution-groups",
        "import-intents",
        "discovery-checkpoints",
        "pending-attempts",
        "sources",
        "source-revisions",
        "discovery-intents",
        "units",
        "unit-revisions",
        "publications",
        "attempts",
        "products",
        "families",
        "family-defaults",
        "annotations",
        "views",
        "normalizations",
        "version-reviews",
        "metadata-corrections",
    }:
        raise ValueError("Unknown source catalog collection")
    path = kb_dir / ".openkb/catalog" / collection / f"{identity}.json"
    contained_paths(kb_dir, [path])
    return path


def read_record(kb_dir: Path, collection: str, identity: str, model: type[R]) -> R:
    record = model.model_validate_json(record_path(kb_dir, collection, identity).read_text("utf-8"))
    field = {
        "workbooks": "source_revision_id",
        "execution-groups": "root_import_id",
        "import-intents": "intent_id",
        "discovery-checkpoints": "checkpoint_id",
        "pending-attempts": "attempt_id",
        "sources": "source_id",
        "source-revisions": "source_revision_id",
        "discovery-intents": "intent_id",
        "units": "unit_id",
        "unit-revisions": "unit_revision_id",
        "publications": "publication_id",
        "attempts": "attempt_id",
        "products": "product_id",
        "families": "family_id",
        "family-defaults": "family_id",
        "annotations": "annotation_id",
        "views": "view_id",
        "normalizations": "normalization_id",
        "version-reviews": "review_id",
        "metadata-corrections": "correction_id",
    }[collection]
    if getattr(record, field) != identity:
        raise ValueError("Catalog record identity does not match its filename")
    return record


def write_record(path: Path, record: Record) -> None:
    """Caller includes this path in its mutation before writing."""
    atomic_write_json(path, record.model_dump(mode="json"), ensure_ascii=False)


def list_sources(kb_dir: Path) -> tuple[Source, ...]:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        return tuple(
            read_record(root, "sources", path.stem, Source)
            for path in sorted((root / ".openkb/catalog/sources").glob("*.json"))
        )


def read_source_revision(kb_dir: Path, identity: str) -> SourceRevision:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        return read_record(root, "source-revisions", identity, SourceRevision)


def read_source(kb_dir: Path, identity: str) -> Source:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        return read_record(root, "sources", identity, Source)


@dataclass(frozen=True)
class Admission:
    source: Source
    revision: SourceRevision
    discovery_intent: DiscoveryIntent


def read_admission(kb_dir: Path, source: Source) -> Admission:
    """Read a current admission only after checking its retained-input associations."""
    revision = read_source_revision(kb_dir, source.target_revision_id)
    intent = read_record(kb_dir, "discovery-intents", revision.discovery_intent_id, DiscoveryIntent)
    if (
        revision.source_id != source.source_id
        or intent.source_revision_id != revision.source_revision_id
        or intent.original != revision.original
    ):
        raise ValueError("Source, revision and discovery intent do not identify the same input")
    return Admission(source, revision, intent)


def admit_source_revision(
    kb_dir: Path,
    prepared: PreparedInput,
    *,
    identity: str | None = None,
    check_stop: Callable[[], None] = lambda: None,
    legacy_hash: str | None = None,
    doc_name: str | None = None,
    name: str | None = None,
    original_kind: Literal["original", "legacy_snapshot"] = "original",
    execution=None,
    reprocess_from: str | None = None,
    reprocessing_request: str | None = None,
    reprocessing_policy: str | None = None,
    text_assessment=None,
) -> Admission:
    """Commit original, related assets, target revision and discovery as one unit."""
    root = kb_dir.resolve()
    from openkb.import_text import preflight_import_text, validate_text_preflight

    text_assessment = text_assessment or preflight_import_text(
        root, prepared, check_stop=check_stop
    )
    validate_text_preflight(prepared, text_assessment)
    identity = identity or _portable_path(prepared.identity, root)
    with kb_ingest_lock(root / ".openkb"):
        check_stop()
        generation = current_generation(root)
        sources = list_sources(root)
        known = next((source for source in sources if source.identity == identity), None)
        source_id = known.source_id if known else uuid.uuid4().hex
        revision_id, intent_id = uuid.uuid4().hex, uuid.uuid4().hex
        original = f".openkb/artifacts/{prepared.digest}/content{prepared.source.suffix.lower()}"
        copies = {root / original: prepared.path}
        assets = []
        for reference, image in sorted(prepared.images.items()):
            artifact = f".openkb/artifacts/{image.digest}/content" if image.digest else None
            assets.append(
                FrozenAsset(original_reference=reference, artifact=artifact, digest=image.digest)
            )
            if artifact and image.path:
                copies[root / artifact] = image.path
        contained_paths(root, list(copies))
        for path in copies:
            if path.exists() and HashRegistry.hash_file(path) != path.parent.name:
                raise ValueError("Frozen artifact failed its content digest check")
        if known and not known.removed:
            previous = read_source_revision(root, known.target_revision_id)
            if previous.source_id != known.source_id:
                raise ValueError("Source revision belongs to another document")
            if reprocess_from and (
                previous.source_revision_id != reprocess_from
                or previous.digest != prepared.digest
                or previous.assets != tuple(assets)
                or previous.original_kind != "original"
                or previous.source_format != prepared.source.suffix.lower().lstrip(".")
            ):
                raise ValueError("Reprocessing requires the unchanged frozen original and assets")
            if (
                not reprocess_from
                and previous.digest == prepared.digest
                and previous.assets == tuple(assets)
                and previous.original_kind == original_kind
                and previous.source_format == prepared.source.suffix.lower().lstrip(".")
            ):
                intent = read_record(
                    root, "discovery-intents", previous.discovery_intent_id, DiscoveryIntent
                )
                missing = [path for path in copies if not path.exists()]
                if missing:
                    contained_paths(root, missing)
                    with mutation_scope(root, missing, operation="restore-frozen-input"):
                        for path in missing:
                            _copy_file_atomic(copies[path], path)
                        check_stop()
                return Admission(known, previous, intent)
        elif reprocess_from:
            raise ValueError("Reprocessing requires a current source")
        requested_name = doc_name
        doc_name = known.doc_name if known else doc_name or _sanitize_stem(prepared.source.stem)
        if not known and requested_name is None:
            occupied = occupied_document_names(root)
            if doc_name in occupied:
                doc_name += f"-{source_id[:8]}"
        source = Source(
            source_id=source_id,
            identity=identity,
            name=name or prepared.source.name,
            doc_name=doc_name,
            target_revision_id=revision_id,
            target_generation=(known.target_generation + 1 if known else 1),
            legacy_hash=legacy_hash or (known.legacy_hash if known else None),
            annotation_id=known.annotation_id if known else None,
            family_id=known.family_id if known else None,
            excluded_inputs=known.excluded_inputs if known else {},
        )
        decoding = None
        if (
            prepared.source.suffix.lower() in {".txt", ".csv", ".xml", ".html", ".htm"}
            and original_kind == "original"
        ):
            from openkb.text_encoding import inspect_text_encoding

            decoding = inspect_text_encoding(
                prepared.path.read_bytes(), source_format=prepared.path.suffix[1:].lower()
            )
        revision = SourceRevision(
            reprocessing_request=reprocessing_request,
            reprocessing_policy=reprocessing_policy,
            reprocessed_from=reprocess_from,
            text_decoding=decoding,
            source_revision_id=revision_id,
            source_id=source_id,
            discovery_intent_id=intent_id,
            original=original,
            digest=prepared.digest,
            source_format=prepared.source.suffix.lower().lstrip("."),
            assets=tuple(assets),
            created_at=datetime.now(timezone.utc).isoformat(),
            original_kind=original_kind,
        )
        from openkb.pending.policies import CURRENT_POLICY, discovery_hosts

        discovery_policy = execution.discovery_policy if execution else CURRENT_POLICY
        intent = DiscoveryIntent(
            intent_id=intent_id,
            source_revision_id=revision_id,
            original=original,
            root_import_id=execution.root_import_id if execution else uuid.uuid4().hex,
            kb_generation=generation,
            status="completed"
            if original_kind == "legacy_snapshot"
            or revision.source_format not in discovery_hosts(discovery_policy)
            else "pending",
            depth=execution.depth if execution else 0,
            ancestry=execution.ancestry if execution else (),
            policy=discovery_policy,
        )
        records = {
            catalog_schema_path(root): CatalogSchema(),
            record_path(root, "sources", source_id): source,
            record_path(root, "source-revisions", revision_id): revision,
            record_path(root, "discovery-intents", intent_id): intent,
        }
        from openkb.pending.records import ExecutionBudget, ExecutionGroup

        group_path = record_path(root, "execution-groups", intent.root_import_id)
        if execution:
            group = read_record(root, "execution-groups", intent.root_import_id, ExecutionGroup)
            if (
                group.cancelled
                or execution.kb_generation != generation
                or group.kb_generation != generation
            ):
                raise ValueError(
                    "Execution group was cancelled or belongs to an old knowledge base"
                )
        else:
            from openkb.config import resolve_effective_config

            effective, origins = resolve_effective_config(root)
            records[group_path] = ExecutionGroup(
                root_import_id=intent.root_import_id,
                kb_generation=generation,
                budget=ExecutionBudget.model_validate(effective.get("extraction_budget") or {}),
                budget_origin=origins["extraction_budget"],
            )
        from openkb.source_changes import source_change_heads

        records.update(source_change_heads(root, source, "updated"))
        contained_paths(root, list(copies))
        new_artifacts = [path for path in copies if not path.exists()]
        with mutation_scope(root, [*new_artifacts, *records], operation="admit-source-revision"):
            for path in new_artifacts:
                _copy_file_atomic(copies[path], path)
            for path, record in records.items():
                write_record(path, record)
            check_stop()
            if current_generation(root) != generation:
                raise ValueError("Knowledge base generation changed during admission")
        return Admission(source, revision, intent)
