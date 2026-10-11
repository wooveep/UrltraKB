"""Recognize readable old originals while same-original reprocessing is unfinished."""

from pathlib import Path

from openkb.source_catalog import read_source, read_source_revision
from openkb.source_records import SourceRevision


def _same_original(left: SourceRevision, right: SourceRevision) -> bool:
    return all(
        getattr(left, key) == getattr(right, key)
        for key in ("source_id", "original", "digest", "source_format", "assets", "original_kind")
    )


def retained_reprocessing_input(kb_dir: Path, view_id: str, identity: str) -> dict | None:
    """Allow only frozen native reading, never restore current contribution status."""
    from openkb.knowledge_evidence import validated_input
    from openkb.state import HashRegistry

    _, frozen = validated_input(kb_dir, identity, view_id)
    source = read_source(kb_dir, frozen.source_id)
    if (
        source.removed
        or source.contribution_empty
        or identity in source.excluded_inputs
        or source.target_revision_id == frozen.source_revision_id
    ):
        return None
    target = read_source_revision(kb_dir, source.target_revision_id)
    revision, seen = target, set()
    while revision.source_revision_id != frozen.source_revision_id:
        if (
            revision.source_revision_id in seen
            or not _same_original(revision, frozen)
            or not revision.reprocessed_from
            or not revision.reprocessing_request
        ):
            return None
        seen.add(revision.source_revision_id)
        revision = read_source_revision(kb_dir, revision.reprocessed_from)
    original = (kb_dir / frozen.original).resolve()
    if (
        not original.is_relative_to(kb_dir.resolve())
        or not original.is_file()
        or HashRegistry.hash_file(original) != frozen.digest
    ):
        return None
    return {
        "processing_status": "retained_before_reprocessing",
        "target_source_revision_id": target.source_revision_id,
        "interpretation": (
            "The same original is being reprocessed. Read this retained successful processing "
            "result with its recorded source revision; it does not certify that the new "
            "processing or dependent knowledge refresh has completed."
        ),
    }
