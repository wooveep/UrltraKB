"""Recognize readable old originals while same-original reprocessing is unfinished."""

from pathlib import Path

from ultrakb.source_catalog import read_source, read_source_revision
from ultrakb.source_records import SourceRevision


def _same_original(left: SourceRevision, right: SourceRevision) -> bool:
    return all(
        getattr(left, key) == getattr(right, key)
        for key in ("source_id", "original", "digest", "source_format", "assets", "original_kind")
    )


def retained_reprocessing_input(kb_dir: Path, view_id: str, identity: str) -> dict | None:
    """Allow only frozen native reading, never restore current contribution status."""
    from ultrakb.knowledge_evidence import validated_input
    from ultrakb.state import HashRegistry

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


def retained_source_snapshot(kb_dir, scope, unit) -> tuple[Path, dict[str, str]] | None:
    """Authenticate only native files from this snapshot's exact published input."""
    from ultrakb.application.sources import _snapshot_source
    from ultrakb.source_map import read_source_map
    from ultrakb.state import HashRegistry

    saved = _snapshot_source(kb_dir, scope, unit.unit_id)
    if saved is None or saved[1].source_map is None:
        return None
    root, reference = saved[0] / "wiki", saved[1].source_map
    read_source_map(root, reference, unit.doc_name)
    files = {reference.path: reference.digest, **reference.assets}
    if reference.unit_kind in {"text", "block"}:
        body = f"sources/{unit.doc_name}.md"
        files[body] = HashRegistry.hash_file(root / body)
    elif saved[1].normalized_source == f"sources/{unit.doc_name}.md":
        from ultrakb.file_state import contained_paths

        body = saved[1].normalized_source
        contained_paths(root, [root / body])
        digest = saved[1].generated_baselines.get(body)
        if digest is None or HashRegistry.hash_file(root / body) != digest:
            raise ValueError("Published native body has no matching recorded digest")
        files[body] = digest
    return root, files


def retained_file_fallbacks(
    kb_dir, scope, unit
) -> tuple[dict[str, Path], dict[str, str], set[str]]:
    """Bind missing native files to their proven ancestor without altering either wiki."""
    from ultrakb.file_state import contained_paths
    from ultrakb.state import HashRegistry

    saved = retained_source_snapshot(kb_dir, scope, unit)
    roots: dict[str, Path] = {}
    files: dict[str, str] = {}
    if saved is None:
        return roots, files, set()
    source, expected = saved
    for name, digest in expected.items():
        target = scope.wiki_dir / name
        contained_paths(scope.wiki_dir, [target])
        if target.is_symlink():
            raise ValueError("Retained native input cannot be a symlink")
        if target.exists():
            if HashRegistry.hash_file(target) != digest:
                raise ValueError("Retained native input differs from its published source")
        else:
            roots[name], files[name] = source, digest
    return roots, files, set(expected)


def restore_retained_originals(kb_dir, wiki, head, replacing_unit_id) -> None:
    """Restore excluded native inputs after generation, before the caller publishes."""
    from ultrakb.application.views import view_scope
    from ultrakb.file_state import contained_paths
    from ultrakb.ingest_records import ImportUnit
    from ultrakb.mutation import _copy_file_atomic
    from ultrakb.source_catalog import read_record
    from ultrakb.state import HashRegistry

    if head.knowledge_revision_id is None:
        return
    scope = view_scope(kb_dir, head.view_id, historical_revision=head.knowledge_revision_id)
    for unit_id, identity in head.inputs.items():
        if unit_id == replacing_unit_id or not retained_reprocessing_input(
            kb_dir, head.view_id, identity
        ):
            continue
        unit = read_record(kb_dir, "units", unit_id, ImportUnit)
        saved = retained_source_snapshot(kb_dir, scope, unit)
        if saved is None:
            continue
        source, files = saved
        for name, digest in files.items():
            target = wiki / name
            contained_paths(wiki, [target])
            if target.is_symlink():
                raise ValueError("Retained native input cannot be a symlink")
            if target.exists():
                if HashRegistry.hash_file(target) != digest:
                    raise ValueError("Retained native input changed during compilation")
            else:
                _copy_file_atomic(source / name, target)
