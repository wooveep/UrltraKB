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


def record_path(kb_dir: Path, collection: str, identity: str) -> Path:
    TypeAdapter(RecordId).validate_python(identity)
    if collection not in {
        "sources",
        "source-revisions",
        "discovery-intents",
        "units",
        "unit-revisions",
        "publications",
        "attempts",
        "products",
        "families",
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
        "sources": "source_id",
        "source-revisions": "source_revision_id",
        "discovery-intents": "intent_id",
        "units": "unit_id",
        "unit-revisions": "unit_revision_id",
        "publications": "publication_id",
        "attempts": "attempt_id",
        "products": "product_id",
        "families": "family_id",
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
) -> Admission:
    """Commit original, related assets, target revision and discovery as one unit."""
    root = kb_dir.resolve()
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
            if previous.digest == prepared.digest and previous.assets == tuple(assets):
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
        requested_name = doc_name
        doc_name = known.doc_name if known else doc_name or _sanitize_stem(prepared.source.stem)
        if not known and requested_name is None:
            legacy = HashRegistry(root / ".openkb/hashes.json").all_entries()
            occupied = {source.doc_name for source in sources} | {
                meta.get("doc_name") or Path(meta.get("name", "")).stem for meta in legacy.values()
            }
            if doc_name in occupied:
                doc_name += f"-{source_id[:8]}"
        source = Source(
            source_id=source_id,
            identity=identity,
            name=name or prepared.source.name,
            doc_name=doc_name,
            target_revision_id=revision_id,
            target_generation=(known.target_generation + 1 if known else 1),
            legacy_hash=legacy_hash,
            annotation_id=known.annotation_id if known else None,
            family_id=known.family_id if known else None,
        )
        revision = SourceRevision(
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
        intent = DiscoveryIntent(
            intent_id=intent_id,
            source_revision_id=revision_id,
            original=original,
            root_import_id=uuid.uuid4().hex,
            kb_generation=generation,
            status="completed" if original_kind == "legacy_snapshot" else "pending",
        )
        records = {
            catalog_schema_path(root): CatalogSchema(),
            record_path(root, "sources", source_id): source,
            record_path(root, "source-revisions", revision_id): revision,
            record_path(root, "discovery-intents", intent_id): intent,
        }
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
