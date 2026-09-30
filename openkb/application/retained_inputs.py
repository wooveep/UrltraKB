"""Validate and reconstruct retained inputs without reading the external source path."""

from pathlib import Path
from urllib.parse import unquote

from openkb.file_state import contained_paths
from openkb.inputs import PreparedImage, PreparedInput
from openkb.state import HashRegistry


def artifact_availability(kb_dir, relative, digest):
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


def frozen_source_input(kb_dir, source, revision):
    path = kb_dir / revision.original
    if not artifact_availability(kb_dir, revision.original, revision.digest)["available"] or any(
        asset.digest
        and not artifact_availability(kb_dir, asset.artifact, asset.digest)["available"]
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
