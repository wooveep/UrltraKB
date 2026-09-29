"""Retain a conversion independently of version clarification and model work."""

import hashlib
import json
from pathlib import Path
from typing import Callable

from openkb.config import resolve_effective_config
from openkb.converter import ConvertResult, convert_document
from openkb.file_state import contained_paths
from openkb.inputs import PreparedInput
from openkb.mutation import _copy_file_atomic, mutation_scope
from openkb.source_catalog import Admission, read_record, record_path, write_record
from openkb.source_records import Digest, Record, RecordId, RelativePath
from openkb.unit_publication import copy_tree, wiki_versions


class NormalizedInput(Record):
    normalization_id: RecordId
    source_revision_id: RecordId
    fingerprint: str
    raw_path: RelativePath
    source_path: RelativePath | None
    is_long: bool
    files: dict[RelativePath, Digest]


def normalization_fingerprint(kb_dir: Path) -> str:
    config = resolve_effective_config(kb_dir)[0]
    return json.dumps(
        {
            "pipeline": "pdf-physical-v2",
            "threshold": config.get("pageindex_threshold", 20),
            "index_model": config.get("model"),
            "index_policy": "content-based-physical-v1",
        },
        sort_keys=True,
    )


def normalization_id(admission: Admission, fingerprint: str) -> str:
    return hashlib.sha256(
        f"{admission.revision.source_revision_id}:{fingerprint}".encode()
    ).hexdigest()[:32]


def read_normalization(kb_dir: Path, identity: str) -> tuple[Path, NormalizedInput]:
    saved = read_record(kb_dir, "normalizations", identity, NormalizedInput)
    directory = kb_dir / ".openkb/normalized" / identity
    contained_paths(kb_dir, [directory])
    if wiki_versions(kb_dir, directory) != saved.files:
        raise ValueError("Retained normalization changed or is incomplete")
    if saved.raw_path not in saved.files or (
        saved.source_path and saved.source_path not in saved.files
    ):
        raise ValueError("Retained normalization is missing its body")
    return directory, saved


def retain_normalization(
    kb_dir: Path,
    admission: Admission,
    prepared: PreparedInput,
    fingerprint: str,
    *,
    check_stop: Callable[[], None] = lambda: None,
) -> tuple[Path, NormalizedInput]:
    identity = normalization_id(admission, fingerprint)
    record = record_path(kb_dir, "normalizations", identity)
    if record.exists():
        directory, saved = read_normalization(kb_dir, identity)
        if (
            saved.source_revision_id != admission.revision.source_revision_id
            or saved.fingerprint != fingerprint
        ):
            raise ValueError("Normalization belongs to another input")
        return directory, saved
    directory = kb_dir / ".openkb/normalized" / identity
    contained_paths(kb_dir, [directory])
    with mutation_scope(kb_dir, [directory, record], operation="retain-normalization"):
        converted = convert_document(
            prepared.source,
            kb_dir,
            staging_dir=directory,
            prepared=prepared,
            doc_name=admission.source.doc_name,
        )
        if converted.raw_path is None:
            raise ValueError("Conversion did not retain its input")
        saved = NormalizedInput(
            normalization_id=identity,
            source_revision_id=admission.revision.source_revision_id,
            fingerprint=fingerprint,
            raw_path=converted.raw_path.relative_to(directory).as_posix(),
            source_path=converted.source_path.relative_to(directory).as_posix()
            if converted.source_path
            else None,
            is_long=converted.is_long_doc,
            files=wiki_versions(kb_dir, directory),
        )
        write_record(record, saved)
        check_stop()
    return directory, saved


def restore_normalization(directory: Path, saved: NormalizedInput, working: Path) -> ConvertResult:
    copy_tree(directory, working)
    return ConvertResult(
        raw_path=working / saved.raw_path,
        source_path=working / saved.source_path if saved.source_path else None,
        is_long_doc=saved.is_long,
    )


def retain_published_normalization(kb_dir: Path, admission: Admission, unit, view_id: str) -> str:
    """Upgrade previously published inputs without parsing or relabeling their knowledge."""
    from openkb.application.sources import _manifest
    from openkb.ingest_records import UnitRevision
    from openkb.state import HashRegistry
    from openkb.unit_publication import read_unit_publication

    used = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
    identity = normalization_id(admission, used.processing_fingerprint)
    path = record_path(kb_dir, "normalizations", identity)
    if path.exists():
        read_normalization(kb_dir, identity)
        return identity
    state = read_unit_publication(kb_dir, unit.unit_id, view_id)
    actual = _manifest(kb_dir, state)
    if (
        actual is None
        or state.successful_revision_id != used.unit_revision_id
        or used.source_revision_id != admission.revision.source_revision_id
    ):
        raise ValueError("No retained normalization for the current source revision")
    published, manifest = actual
    original = kb_dir / admission.revision.original
    if HashRegistry.hash_file(original) != admission.revision.digest:
        raise ValueError("Retained original changed")
    directory = kb_dir / ".openkb/normalized" / identity
    raw = f"raw/{unit.doc_name}.{admission.revision.source_format}"
    source = f"wiki/{manifest.normalized_source}" if manifest.execution_mode == "full" else None
    if source and manifest.normalized_source is None:
        raise ValueError("Published normalization has no source body")
    copies = {directory / raw: original}
    if source:
        copies[directory / source] = published / source
    if manifest.source_map:
        from openkb.source_map import read_source_map

        read_source_map(published / "wiki", manifest.source_map, unit.doc_name)
        source_map = f"wiki/{manifest.source_map.path}"
        copies[directory / source_map] = published / source_map
    images = f"wiki/sources/images/{unit.doc_name}"
    contained_paths(kb_dir, [directory, *copies.values(), published / images])
    with mutation_scope(kb_dir, [directory, path], operation="retain-published-normalization"):
        for target, origin in copies.items():
            _copy_file_atomic(origin, target)
        if (published / images).exists():
            copy_tree(published / images, directory / images)
        saved = NormalizedInput(
            normalization_id=identity,
            source_revision_id=admission.revision.source_revision_id,
            fingerprint=used.processing_fingerprint,
            raw_path=raw,
            source_path=source,
            is_long=manifest.execution_mode == "segmented",
            files=wiki_versions(kb_dir, directory),
        )
        write_record(path, saved)
    return identity
