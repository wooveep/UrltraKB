"""Complete private Markdown baseline from a fixed immutable publication."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ultrakb.locks import PrivateMaterialization

from .blobs import Blob, materialize_private
from .coordination import storage_stage
from .navigation_store import require_navigation

if TYPE_CHECKING:
    from .knowledge_store import Publication


@dataclass(frozen=True)
class CompileBaseline:
    directory: Path
    publication: "Publication"
    files: dict[str, str]
    bytes_copied: int
    private: PrivateMaterialization


@contextmanager
def materialize_baseline(bound, required_attachments):
    if not isinstance(required_attachments, tuple) or any(
        not isinstance(path, str) for path in required_attachments
    ):
        raise ValueError("Compile attachments must be explicit logical paths")
    if any(
        PurePosixPath(path).suffix.lower() in {".db", ".sqlite", ".sqlite3"}
        for path in required_attachments
    ):
        raise ValueError("Index databases cannot be compile inputs")
    with PrivateMaterialization() as private:
        with storage_stage(bound.root, bound.identity, read_only=True) as db:
            require_navigation(db, bound.publication.revision_id)
            rows = db.connection.execute(
                "SELECT f.path, f.digest, f.byte_count FROM ukb_publications p "
                "JOIN ukb_revision_files f ON f.revision_id = p.revision_id "
                "WHERE p.publication_id = ? AND p.revision_id = ? AND p.view_id = ? "
                "ORDER BY f.path",
                (bound.publication_id, bound.publication.revision_id, bound.publication.view_id),
            ).fetchall()
            manifest = {path: Blob(digest, size) for path, digest, size in rows}
            for path in required_attachments:
                if path not in manifest:
                    raise FileNotFoundError(path)
            selected = {path for path in manifest if PurePosixPath(path).suffix.lower() == ".md"}
            selected.update(required_attachments)
            for path in sorted(selected):
                materialize_private(db, manifest[path], private, path)
        # No database connection, reclaim lease or rollback snapshot spans model work.
        yield CompileBaseline(
            private.directory,
            bound.publication,
            {path: blob.digest for path, blob in manifest.items()},
            private.bytes_copied,
            private,
        )
