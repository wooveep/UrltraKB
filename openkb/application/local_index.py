"""Remove unreferenced working index state under the KB's mutation lease."""

from __future__ import annotations

import shutil
from pathlib import Path

from openkb.condb_storage import ConDBPageIndexStorage
from openkb.file_state import contained_paths
from openkb.index_location import IndexLocation
from openkb.locks import kb_ingest_lock_held


def remove_index_document(kb_dir: Path, doc_name: str, doc_id: str | None) -> tuple[bool, str]:
    root = kb_dir / ".openkb"
    if not kb_ingest_lock_held(root):
        raise RuntimeError("Local index cleanup requires the KB write lease")
    location = IndexLocation.package(root)
    if not location.database.exists():
        return False, "no document index state"
    if location.read_only:
        return False, "retained immutable index"
    with ConDBPageIndexStorage(location) as storage:
        candidates = [
            row
            for row in storage.list_documents("default")
            if (row["doc_id"] == doc_id if doc_id else row["doc_name"] == doc_name)
        ]
        if not candidates:
            return False, "no index document to delete"
        if len(candidates) > 1:
            raise ValueError(f"Multiple index documents match doc_name='{doc_name}'")
        identity = candidates[0]["doc_id"]
        from openkb.artifact_references import list_artifact_references

        references = list_artifact_references(kb_dir)
        if not references.complete or identity in references.index_documents:
            return False, "retained index evidence; referenced or ownership incomplete"
        document = storage.get_document("default", identity)
        blob = Path(document["file_path"])
        images = location.inputs_root / "default" / identity
        contained_paths(location.inputs_root, [blob, images])
        storage.delete_document("default", identity)
        blob.unlink(missing_ok=True)
        if images.exists():
            shutil.rmtree(images)
    return True, f"deleted index document ({identity[:12]}…)"
