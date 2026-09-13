"""Immutable source/parse bindings inside the same PageIndex SQLite database."""

import json
import sqlite3
from contextlib import closing

from openkb.sources import content_id, valid_id


def save_binding(database, record):
    identity = content_id(record)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS openkb_source_indexes (
                index_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_version TEXT NOT NULL,
                parse_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                doc_id TEXT NOT NULL UNIQUE REFERENCES documents(doc_id) ON DELETE CASCADE,
                metadata TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS openkb_index_lookup
            ON openkb_source_indexes(source_version, parse_id, profile);
        """)
        connection.execute(
            "INSERT INTO openkb_source_indexes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                identity,
                record["source_id"],
                record["version"],
                record["parse"],
                record["profile"],
                record["pageindex"]["doc_id"],
                json.dumps(record),
            ),
        )
    return identity


def index_bindings(database, *, identity=None, version=None, parse=None, profile=None):
    if not database.is_file():
        return {}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='openkb_source_indexes'"
        ).fetchone():
            return {}
        conditions, values = [], []
        for column, value in (
            ("index_id", identity),
            ("source_version", version),
            ("parse_id", parse),
            ("profile", profile),
        ):
            if value is not None:
                conditions.append(column + " = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        limit = " LIMIT 1" if conditions else ""
        rows = connection.execute(
            "SELECT index_id, metadata FROM openkb_source_indexes"
            + where
            + " ORDER BY rowid DESC"
            + limit,
            values,
        ).fetchall()
    result = {}
    for identity, metadata in rows:
        record = json.loads(metadata)
        if not isinstance(record, dict):
            raise ValueError("Invalid PageIndex binding metadata")
        result[identity] = record
    return result


def binding_inventory(database):
    """Trace relational provenance without decoding potentially damaged retired metadata."""
    if not database.is_file():
        return {}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='openkb_source_indexes'"
        ).fetchone():
            return {}
        rows = connection.execute(
            "SELECT index_id, source_id, source_version, parse_id, doc_id, metadata "
            "FROM openkb_source_indexes"
        ).fetchall()
    return {
        valid_id(identity): {
            "source_id": valid_id(source, source=True),
            "version": valid_id(version),
            "parse": valid_id(parse),
            "pageindex": {"doc_id": doc_id},
            "metadata_digest": content_id(metadata),
        }
        for identity, source, version, parse, doc_id, metadata in rows
    }
