"""Explicit query publication pointers, independent of knowledge compilation status."""

import sqlite3
from contextlib import closing

from openkb.locks import kb_ingest_lock_held, kb_read_lock
from openkb.pageindex_store import database_paths
from openkb.sources import SourceStore, valid_id


def _source_states(kb_dir):
    with kb_read_lock(kb_dir / ".openkb"):
        database = database_paths(kb_dir)[0]
        if not database.is_file():
            return {}
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='openkb_query_sources'"
            ).fetchone():
                return {}
            rows = connection.execute(
                "SELECT source_id, source_version, parse_id, navigation_id "
                "FROM openkb_query_sources ORDER BY source_id"
            ).fetchall()
        return {
            valid_id(source, source=True): {
                "source_id": source,
                "source_version": valid_id(version),
                "parse_id": valid_id(parse) if parse is not None else None,
                "navigation_id": valid_id(navigation) if navigation is not None else None,
            }
            for source, version, parse, navigation in rows
        }


def query_source_bindings(kb_dir):
    """Read activated bindings only; never discover arbitrary selected parses."""
    return {
        source: row
        for source, row in _source_states(kb_dir).items()
        if row["navigation_id"] is not None
    }


def source_withdrawn(kb_dir, source):
    """A removed current version stays in history until explicitly activated again."""
    row = _source_states(kb_dir).get(source.source_id)
    return bool(row and row["source_version"] == source.id and row["navigation_id"] is None)


def _ensure_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS openkb_query_sources (
            source_id TEXT PRIMARY KEY,
            source_version TEXT NOT NULL,
            parse_id TEXT,
            navigation_id TEXT REFERENCES openkb_source_indexes(index_id),
            CHECK ((parse_id IS NULL) = (navigation_id IS NULL))
        )
    """)


def bind_query_source(kb_dir, source, parsed, navigation):
    """Caller holds a resource+database mutation; this is not a compilation receipt."""
    if not kb_ingest_lock_held(kb_dir / ".openkb"):
        raise RuntimeError("Query source activation requires the KB write lease")
    with closing(sqlite3.connect(database_paths(kb_dir)[0])) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _ensure_table(connection)
        bound = connection.execute(
            "SELECT source_id, source_version, parse_id FROM openkb_source_indexes "
            "WHERE index_id=?",
            (navigation["id"],),
        ).fetchone()
        if bound != (source.source_id, source.id, parsed.id):
            raise ValueError("Query binding differs from the saved navigation identity")
        connection.execute(
            "INSERT INTO openkb_query_sources VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET source_version=excluded.source_version, "
            "parse_id=excluded.parse_id, navigation_id=excluded.navigation_id",
            (source.source_id, source.id, parsed.id, navigation["id"]),
        )


def unbind_query_source(kb_dir, source_id):
    """Withdraw visibility while retained citations and conversation snapshots remain valid."""
    valid_id(source_id, source=True)
    if not kb_ingest_lock_held(kb_dir / ".openkb"):
        raise RuntimeError("Query source removal requires the KB write lease")
    source = SourceStore(kb_dir).current(source_id)
    with closing(sqlite3.connect(database_paths(kb_dir)[0])) as connection, connection:
        _ensure_table(connection)
        connection.execute(
            "INSERT INTO openkb_query_sources VALUES (?, ?, NULL, NULL) "
            "ON CONFLICT(source_id) DO UPDATE SET source_version=excluded.source_version, "
            "parse_id=NULL, navigation_id=NULL",
            (source_id, source.id),
        )
