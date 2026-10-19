"""Prepare an owned local copy of one immutable retained source file."""

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Literal

from ultrakb.storage.database import DatabaseIdentity


@dataclass
class SourceFile:
    path: Path
    _directory: TemporaryDirectory[str] = field(repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def request_stop(self) -> None:
        # Preparation has finished; no reader remains attached to the KB.
        pass

    def close(self) -> None:
        with self._lock:
            self._directory.cleanup()


def prepare_source_file(
    root: Path,
    source_id: str,
    unit_revision_id: str,
    kind: Literal["original", "pdf"],
    *,
    identity: DatabaseIdentity,
    cancelled=lambda: False,
) -> SourceFile:
    """Copy the selected original or verified conversion PDF under the GC lease."""
    from ultrakb.storage.blobs import copy_blob
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.coordination import storage_stage

    from .storage_conversion_metadata import conversion_metadata

    with storage_stage(root, identity, read_only=True, cancelled=cancelled) as db:
        catalog = CatalogStore(root, identity)
        unit = catalog.unit_revision(unit_revision_id)
        if unit.source_id != source_id:
            raise ValueError("The retained file belongs to another source")
        if kind == "original":
            revision = catalog.revision(unit.source_revision_id)
            selected = next(file for file in revision.files if file.kind == "main")
        elif kind == "pdf":
            processing = catalog.processing(unit.processing_id)
            if not conversion_metadata(catalog, unit, processing):
                raise FileNotFoundError("This processing revision has no converted PDF")
            [selected] = [
                file
                for file in processing.files
                if file.path.startswith("conversion/") and file.path.endswith(".pdf")
            ]
        else:
            raise ValueError("Select the retained original or converted PDF")
        directory = TemporaryDirectory(prefix="ultrakb-source-")
        try:
            path = Path(directory.name) / PurePosixPath(selected.path).name
            with path.open("xb") as output:
                copy_blob(db, selected.digest, output, cancelled=cancelled)
            if cancelled():
                raise InterruptedError("Source file preparation cancelled")
            return SourceFile(path, directory)
        except BaseException:
            directory.cleanup()
            raise
