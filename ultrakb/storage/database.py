"""Explicit v2 creation, validated connections and independent durable commits.

Opening never creates, migrates or repairs a database. All connections belong
to their creating thread; borrowed SDK connections retain that ownership.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ultrakb.locks import _fsync_directory
from ultrakb.storage.schema import SCHEMA

FORMAT = "ultrakb.context-kb"
FORMAT_VERSION = 2
SCHEMA_VERSION = 18
WRITER_VERSION = 1
CAPABILITIES = frozenset({"cas-v1", "independent-commits-v1", "condb-tree-scope-v2"})
BUSY_TIMEOUT_MS = 2_000

_WRITING: ContextVar[frozenset[Path]] = ContextVar("storage_writing", default=frozenset())


class StorageFormatError(ValueError):
    """Unsupported or incomplete storage; original data must remain unchanged."""


class UnsupportedSQLite(StorageFormatError):
    """The actual linked runtime lacks a verified WAL-reset fix."""


@dataclass(frozen=True)
class DatabaseIdentity:
    kb_id: str
    generation: str


def require_sqlite() -> str:
    """Check the linked library, including the two documented backport series."""
    try:
        version = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    except ValueError:
        version = ()
    supported = len(version) == 3 and (
        version >= (3, 51, 3)
        or version[:2] == (3, 50)
        and version[2] >= 7
        or version[:2] == (3, 44)
        and version[2] >= 6
    )
    if not supported:
        raise UnsupportedSQLite(
            f"SQLite {sqlite3.sqlite_version} lacks a verified WAL-reset fix; "
            "use a supported runtime before creating or writing a knowledge base"
        )
    probe = sqlite3.connect(":memory:")
    try:
        _preserve_wal(probe)
    finally:
        probe.close()
    return sqlite3.sqlite_version


def _preserve_wal(connection: sqlite3.Connection) -> None:
    flag = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
    configure = getattr(connection, "setconfig", None)
    inspect = getattr(connection, "getconfig", None)
    if flag is None or not callable(configure) or not callable(inspect):
        raise UnsupportedSQLite("SQLite bindings must support disabling last-close checkpoints")
    try:
        configure(flag, True)
        if inspect(flag) is not True:
            raise UnsupportedSQLite("The SQLite runtime cannot preserve WAL on client close")
    except sqlite3.Error as error:
        raise UnsupportedSQLite(
            "The SQLite runtime cannot control client-close checkpoints"
        ) from error


def database_path(root: Path) -> Path:
    return root.resolve() / ".ultrakb/kb.sqlite"


def _validate(connection: sqlite3.Connection, expected=None) -> DatabaseIdentity:
    try:
        row = connection.execute(
            "SELECT format, format_version, schema_version, minimum_writer_version, "
            "required_capabilities, kb_id, generation FROM ukb_meta WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ValueError("Missing format identity")
        name, version, schema, writer, encoded, kb_id, generation = row
        capabilities = json.loads(encoded)
        if (
            name != FORMAT
            or type(version) is not int
            or version != FORMAT_VERSION
            or type(schema) is not int
            or schema != SCHEMA_VERSION
            or type(writer) is not int
            or not 1 <= writer <= WRITER_VERSION
            or not isinstance(capabilities, list)
            or any(not isinstance(item, str) for item in capabilities)
            or not set(capabilities) <= CAPABILITIES
            or not isinstance(kb_id, str)
            or not kb_id
            or not isinstance(generation, str)
            or not generation
        ):
            raise ValueError("Unsupported format, schema, writer or capabilities")
        identity = DatabaseIdentity(kb_id, generation)
        if expected is not None and identity != expected:
            raise ValueError("Knowledge-base identity or generation changed")
        return identity
    except (sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise StorageFormatError(
            "Unsupported knowledge-base storage; reopen a supported knowledge base"
        ) from error


def create_database(root: Path, *, generation: str | None = None) -> DatabaseIdentity:
    """Create a new v2 store; callers must own the creation lifecycle lease.

    This remains a test/development entry point until the P8 enablement gate.
    Existing state is always refused, including pre-v2 configured directories.
    """
    from ultrakb.lifecycle import creation_lifecycle

    require_sqlite()
    with creation_lifecycle(root):
        return _create_database(root, generation=generation)


def _create_database(root: Path, *, generation: str | None) -> DatabaseIdentity:
    root = root.resolve()
    state = root / ".ultrakb"
    if state.is_symlink() or state.exists() and any(state.iterdir()):
        raise StorageFormatError("Storage already exists; creation cannot migrate it")
    state.mkdir(parents=True, exist_ok=True)
    identity = DatabaseIdentity(uuid.uuid4().hex, generation or uuid.uuid4().hex)
    fd, name = tempfile.mkstemp(prefix=".kb-", suffix=".sqlite", dir=state)
    os.close(fd)
    temporary = Path(name)
    connection = sqlite3.connect(temporary, isolation_level=None)
    try:
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE ukb_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            format TEXT NOT NULL,
            format_version INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            minimum_writer_version INTEGER NOT NULL,
            required_capabilities TEXT NOT NULL CHECK (json_valid(required_capabilities)),
            kb_id TEXT NOT NULL UNIQUE,
            generation TEXT NOT NULL
        )""")
        connection.execute(
            "INSERT INTO ukb_meta VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                FORMAT,
                FORMAT_VERSION,
                SCHEMA_VERSION,
                WRITER_VERSION,
                json.dumps(sorted(CAPABILITIES)),
                identity.kb_id,
                identity.generation,
            ),
        )
        connection.execute("""CREATE TABLE ukb_schema_versions (
            component TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK (version > 0)
        )""")
        connection.execute(
            "INSERT INTO ukb_schema_versions VALUES ('ultrakb', ?)", (SCHEMA_VERSION,)
        )
        for statement in SCHEMA:
            connection.execute(statement)
        connection.commit()
        connection.close()
        # The destination cannot already exist: creation is serialized externally.
        target = database_path(root)
        if target.exists():
            raise StorageFormatError("Storage appeared during creation")
        os.replace(temporary, target)
        _activate_database(root, identity)
        _fsync_directory(state)
        _fsync_directory(root)
    finally:
        connection.close()
        temporary.unlink(missing_ok=True)
    return identity


def _activate_database(root: Path, identity: DatabaseIdentity) -> None:
    """Explicitly finalize only a newly created or validated private restore."""
    path = database_path(root)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, isolation_level=None)
    try:
        _preserve_wal(connection)
        _validate(connection, identity)
        connection.execute("PRAGMA synchronous = FULL")
        if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
            raise StorageFormatError("The new active database could not enter WAL mode")
        # Attach the WAL index so normal reads never need to create sidecars
        # before they know whether the active storage format is supported.
        connection.execute("SELECT kb_id FROM ukb_meta WHERE singleton = 1").fetchone()
    finally:
        connection.close()
    for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        with file.open("r+b") as handle:
            os.fsync(handle.fileno())
    _fsync_directory(path.parent)


class Database:
    def __init__(
        self,
        root: Path,
        connection: sqlite3.Connection,
        identity: DatabaseIdentity,
        read_only: bool,
    ) -> None:
        from ultrakb.locks import _Lease

        self.root: Path = root
        self.connection: sqlite3.Connection = connection
        self.identity: DatabaseIdentity = identity
        self.read_only: bool = read_only
        self.storage_lease: _Lease | None = None
        self.storage_owner: tuple | None = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit only a prepared, bounded unit; nested durability is forbidden."""
        if self.read_only:
            raise RuntimeError("Read-only storage cannot begin a write transaction")
        if self.connection.in_transaction or self.root in _WRITING.get():
            raise RuntimeError("Durable storage requires an independent top-level transaction")
        _validate(self.connection, self.identity)
        token = _WRITING.set(_WRITING.get() | {self.root})
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                _validate(self.connection, self.identity)
                yield self.connection
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
        finally:
            _WRITING.reset(token)


@contextmanager
def open_database(
    root: Path,
    *,
    expected: DatabaseIdentity | None = None,
    read_only: bool = False,
    sealed_archive: bool = False,
) -> Iterator[Database]:
    require_sqlite()
    if sealed_archive and not read_only:
        raise ValueError("Sealed archives can only be opened read-only")
    root = root.resolve()
    path = database_path(root)
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise StorageFormatError("No supported v2 storage; creation is explicit")
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    if wal.is_symlink() or shm.is_symlink():
        raise StorageFormatError("SQLite coordination files cannot follow symbolic links")
    if not sealed_archive and (not wal.is_file() or not shm.is_file()):
        raise StorageFormatError("Active WAL coordination is missing; explicit recovery is needed")
    if sealed_archive and wal.exists() and wal.stat().st_size:
        raise StorageFormatError("An active WAL database cannot be opened as a sealed archive")
    readonly_options = "mode=ro&immutable=1" if sealed_archive else "mode=ro"
    # Validation is read-only and precedes any writer PRAGMA or schema changes.
    try:
        probe = sqlite3.connect(
            f"{path.as_uri()}?{readonly_options}", uri=True, isolation_level=None
        )
        try:
            if not sealed_archive:
                _preserve_wal(probe)
            identity = _validate(probe, expected)
            if not sealed_archive and probe.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise StorageFormatError("Active storage requires WAL; explicit recovery is needed")
        finally:
            probe.close()
        mode = readonly_options if read_only else "mode=rw"
        connection = sqlite3.connect(
            f"{path.as_uri()}?{mode}",
            uri=True,
            isolation_level=None,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
    except sqlite3.DatabaseError as error:
        raise StorageFormatError("Knowledge-base storage cannot be opened safely") from error
    try:
        if not sealed_archive:
            _preserve_wal(connection)
        _validate(connection, identity)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if not read_only:
            connection.execute("PRAGMA synchronous = FULL")
        yield Database(root, connection, identity, read_only)
    finally:
        # No automatic checkpoint, scan or schema initialization on close.
        connection.close()
