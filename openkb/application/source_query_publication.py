"""Atomically expose a validated Step 2 snapshot for original-source queries."""

from pathlib import Path
from tempfile import TemporaryDirectory

from openkb.locks import atomic_write_text, kb_ingest_lock
from openkb.mutation import mutation_scope, publish_staged_tree
from openkb.navigation import read_navigation
from openkb.navigation_tree import snapshot_markdown, snapshot_name
from openkb.pageindex_store import database_paths, indexed_reader
from openkb.query_sources import bind_query_source
from openkb.sources import SourceStore


def activate_query_source(kb_dir, source, parsed, navigation_id):
    """Publish assets and the pointer together, preserving the old binding on failure."""
    from openkb.application.document_pipeline import _materialize

    store = SourceStore(kb_dir)
    with kb_ingest_lock(kb_dir / ".openkb"):
        saved = read_navigation(kb_dir, source, identity=navigation_id)
        if saved["parse"] != parsed.id:
            raise ValueError("Query navigation differs from the saved source")
        indexed_reader(kb_dir, source, parsed, saved)
        with TemporaryDirectory(prefix="query-source-", dir=kb_dir / ".openkb") as directory:
            staging = Path(directory)
            # Reuse source rendering and asset naming; publish only the stable snapshot.
            scratch = _materialize(staging, store, source, parsed, "query-snapshot")
            scratch.unlink()
            atomic_write_text(
                staging / "wiki" / snapshot_name(source, parsed),
                snapshot_markdown(kb_dir, source, parsed, asset_root=staging / "wiki/sources"),
            )
            destinations = [
                store.owned_path(kb_dir / path.relative_to(staging))
                for path in staging.rglob("*")
                if path.is_file()
            ]
            with mutation_scope(
                kb_dir, [*database_paths(kb_dir), *destinations], operation="activate query source"
            ) as snapshot:
                snapshot.track_new([staging])
                publish_staged_tree(staging, kb_dir)
                bind_query_source(kb_dir, source, parsed, saved)
