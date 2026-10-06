"""Capture the evidence a legacy registry still has; never invent lost originals."""

from pathlib import Path

from pydantic import TypeAdapter

from openkb.file_state import contained_paths
from openkb.inputs import prepared_input
from openkb.mutation import _copy_file_atomic, mutation_scope
from openkb.source_catalog import (
    Admission,
    admit_source_revision,
    list_sources,
    read_record,
    read_source_revision,
    write_record,
)
from openkb.source_records import DiscoveryIntent, DocName, Record, RecordId, RelativePath, Source
from openkb.unit_publication import copy_tree, plan_import_units


class LegacySnapshot(Record):
    source_revision_id: RecordId
    doc_name: DocName
    normalized_source: RelativePath
    index_ref: str | None = None


def legacy_snapshot(kb_dir: Path, source: Source) -> tuple[Path, LegacySnapshot]:
    directory = kb_dir / ".openkb/legacy-inputs" / source.target_revision_id
    contained_paths(kb_dir, [directory])
    record = LegacySnapshot.model_validate_json((directory / "manifest.json").read_text("utf-8"))
    if record.source_revision_id != source.target_revision_id or record.doc_name != source.doc_name:
        raise ValueError("Legacy normalization belongs to another source")
    return directory, record


def admit_legacy_snapshot(kb_dir: Path, file_hash: str, meta: dict) -> Source:
    """Called under a write lease, before recompiling a legacy document."""
    existing = next((item for item in list_sources(kb_dir) if item.legacy_hash == file_hash), None)
    if existing:
        revision = read_source_revision(kb_dir, existing.target_revision_id)
        intent = read_record(
            kb_dir, "discovery-intents", revision.discovery_intent_id, DiscoveryIntent
        )
        admission = Admission(existing, revision, intent)
        if (kb_dir / ".openkb/legacy-inputs" / existing.target_revision_id).exists():
            legacy_snapshot(kb_dir, existing)
            plan_import_units(kb_dir, admission, "legacy-normalization-v1")
            return existing
    doc_name = meta.get("doc_name") or Path(meta.get("name") or "").stem
    TypeAdapter(DocName).validate_python(doc_name)
    segmented = meta.get("type") in {"long_pdf", "pageindex_cloud"}
    relative = f"sources/{doc_name}.{'json' if segmented else 'md'}"
    source = kb_dir / admission.revision.original if existing else kb_dir / "wiki" / relative
    contained_paths(kb_dir, [source])
    if not source.is_file():
        raise ValueError(
            "Legacy normalized source is missing; original evidence was not reconstructed"
        )
    with prepared_input(source) as prepared:
        admission = (
            admission
            if existing
            else admit_source_revision(
                kb_dir,
                prepared,
                identity=f"legacy:{file_hash}",
                legacy_hash=file_hash,
                doc_name=doc_name,
                name=meta.get("name") or doc_name,
                original_kind="legacy_snapshot",
            )
        )
        directory = kb_dir / ".openkb/legacy-inputs" / admission.revision.source_revision_id
        record = LegacySnapshot(
            source_revision_id=admission.revision.source_revision_id,
            doc_name=doc_name,
            normalized_source=relative,
            index_ref=meta.get("doc_id") if segmented else None,
        )
        with mutation_scope(kb_dir, [directory], operation="capture-legacy-normalization"):
            _copy_file_atomic(prepared.path, directory / "wiki" / relative)
            summary = kb_dir / "wiki/summaries" / f"{doc_name}.md"
            contained_paths(kb_dir, [summary])
            if summary.exists():
                _copy_file_atomic(summary, directory / "wiki/summaries" / summary.name)
            images = kb_dir / "wiki/sources/images" / doc_name
            if images.exists():
                copy_tree(images, directory / "wiki/sources/images" / doc_name)
            if segmented:
                from openkb.index_packages import copy_index_package

                copy_index_package(
                    kb_dir / ".openkb", directory / "index", document_id=record.index_ref
                )
            write_record(directory / "manifest.json", record)
    plan_import_units(kb_dir, admission, "legacy-normalization-v1")
    return admission.source
