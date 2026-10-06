import json
import sqlite3
import time
import uuid
import threading
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from contextdb.logger import get_logger
from .json_values import validate_json

log = get_logger(__name__)


@dataclass
class Node:
    tree_id: str
    node_id: str
    parent_id: Optional[str]
    slot: Optional[str]
    node_type: int
    depth: int
    path: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    attrs_json: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "tree_id": self.tree_id,
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "slot": self.slot,
            "node_type": self.node_type,
            "depth": self.depth,
            "path": self.path,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "attrs_json": self.attrs_json,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        result = {k: v for k, v in result.items() if v is not None}
        if "attrs_json" in result:
            if result["attrs_json"]:
                result["attrs"] = json.loads(result["attrs_json"])
            del result["attrs_json"]
        return result


@dataclass
class Entity:
    entity_id: str
    entity_type: str
    payload_json: str
    created_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "payload": json.loads(self.payload_json),
            "created_at": self.created_at,
        }


@dataclass
class Tree:
    tree_id: str
    root_node_id: str
    created_at: int
    updated_at: int
    meta_json: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "tree_id": self.tree_id,
            "root_node_id": self.root_node_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.meta_json:
            result["meta"] = json.loads(self.meta_json)
        return result


@runtime_checkable
class StorageProtocol(Protocol):
    def create_tree(self, meta: Optional[dict[str, Any]] = None) -> tuple[str, str]: ...
    def get_node(self, tree_id: str, node_id: str) -> Optional[Node]: ...
    def get_children(self, tree_id: str, node_id: str) -> list[Node]: ...
    def get_subtree(
        self, tree_id: str, node_id: str, max_depth: int = 100, with_entities: bool = False
    ) -> list[dict[str, Any]]: ...
    def get_entity(self, tree_id: str, node_id: str) -> Optional[Entity]: ...
    def get_root_id(self, tree_id: str) -> Optional[str]: ...
    def ingest_tree(
        self,
        tree_structure: dict[str, Any],
        entities: Optional[dict[str, dict[str, Any]]] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> str: ...
    def close(self) -> None: ...


class TreeDB:
    OBJECT = 0
    ARRAY = 1
    LEAF = 2
    _IN_QUERY_CHUNK_SIZE = 500

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str = "treedb.sqlite", *, read_only: bool = False):
        self._transaction_lock = threading.RLock()
        self._savepoint_number = 0
        self._closed = False
        self.read_only = read_only
        self.db_path = str(db_path)
        if read_only:
            path = Path(db_path).resolve(strict=True)
            wal = Path(str(path) + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise ValueError("Read-only storage requires a checkpointed, sealed database")
            self.conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, check_same_thread=False)
        else:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA foreign_keys = ON")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if read_only or version or tables:
                if version != self.SCHEMA_VERSION:
                    raise ValueError(f"Unsupported ConDB schema version {version}")
                self._validate_schema()
            elif not read_only:
                self._init_schema()
        except BaseException:
            self.conn.close()
            raise
        log.info(f"TreeDB opened: {db_path}")

    def _validate_schema(self):
        for table, record in (("trees", Tree), ("nodes", Node), ("entities", Entity)):
            columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if columns != set(record.__dataclass_fields__):
                raise ValueError(f"Unsupported ConDB schema in {table}")

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA temp_store = MEMORY")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trees (
                tree_id       TEXT PRIMARY KEY,
                root_node_id  TEXT NOT NULL,
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL,
                meta_json     TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                tree_id      TEXT NOT NULL,
                node_id      TEXT NOT NULL,
                parent_id    TEXT,
                slot         TEXT,
                node_type    INTEGER NOT NULL,
                depth        INTEGER NOT NULL,
                path         TEXT NOT NULL,
                entity_type  TEXT,
                entity_id    TEXT,
                attrs_json   TEXT,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL,
                PRIMARY KEY (tree_id, node_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id    TEXT PRIMARY KEY,
                entity_type  TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at   INTEGER NOT NULL
            )
        """)

        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS nodes_child_unique ON nodes(tree_id, parent_id, slot)")
        cursor.execute("CREATE INDEX IF NOT EXISTS nodes_path_idx ON nodes(tree_id, path)")
        cursor.execute("CREATE INDEX IF NOT EXISTS nodes_parent_idx ON nodes(tree_id, parent_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS nodes_entity_idx ON nodes(tree_id, entity_type, entity_id)")
        cursor.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        self.conn.commit()

    @contextmanager
    def transaction(self):
        """Commit this unit or roll it back, preserving any caller transaction."""
        if self.read_only:
            raise PermissionError("Sealed ConDB storage is read-only")
        with self._transaction_lock:
            self._savepoint_number += 1
            name = f"condb_{self._savepoint_number}"
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield self.conn
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
                raise

    def _ts(self) -> int:
        return int(time.time() * 1000)

    def create_tree(self, meta: Optional[dict[str, Any]] = None) -> tuple[str, str]:
        validate_json(meta)
        tree_id = str(uuid.uuid4())
        root_id = str(uuid.uuid4())
        now = self._ts()

        with self.transaction():
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO trees (tree_id, root_node_id, created_at, updated_at, meta_json) VALUES (?, ?, ?, ?, ?)",
                (tree_id, root_id, now, now, json.dumps(meta) if meta else None),
            )
            cursor.execute(
                "INSERT INTO nodes (tree_id, node_id, parent_id, slot, node_type, depth, path, created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, 0, ?, ?, ?)",
                (tree_id, root_id, self.OBJECT, f"/r/{root_id}", now, now),
            )
        log.debug(f"create_tree: {tree_id[:8]}")
        return tree_id, root_id

    def get_node(self, tree_id: str, node_id: str) -> Optional[Node]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_id))
        row = cursor.fetchone()
        return Node(**dict(row)) if row else None

    def get_children(self, tree_id: str, node_id: str) -> list[Node]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM nodes WHERE tree_id = ? AND parent_id = ? ORDER BY path", (tree_id, node_id))
        return [Node(**dict(row)) for row in cursor.fetchall()]

    def get_children_many(self, tree_id: str, node_ids: list[str]) -> dict[str, list[Node]]:
        """Fetch several sibling lists while preserving slot order within each parent."""
        unique_ids = list(dict.fromkeys(node_ids))
        result: dict[str, list[Node]] = {node_id: [] for node_id in unique_ids}
        cursor = self.conn.cursor()

        for start in range(0, len(unique_ids), self._IN_QUERY_CHUNK_SIZE):
            chunk = unique_ids[start:start + self._IN_QUERY_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"""
                SELECT * FROM nodes
                WHERE tree_id = ? AND parent_id IN ({placeholders})
                ORDER BY parent_id, path
                """,
                [tree_id, *chunk],
            )
            for row in cursor.fetchall():
                node = Node(**dict(row))
                result[node.parent_id].append(node)
        return result

    def get_subtree(
        self, tree_id: str, node_id: str, max_depth: int = 100, with_entities: bool = False
    ) -> list[dict[str, Any]]:
        log.debug(f"get_subtree: {node_id[:8]} depth={max_depth}")
        cursor = self.conn.cursor()
        cursor.execute("SELECT path, depth FROM nodes WHERE tree_id = ? AND node_id = ?", (tree_id, node_id))
        root_row = cursor.fetchone()
        if not root_row:
            return []

        root_path, root_depth = root_row["path"], root_row["depth"]
        cursor.execute(
            """
            SELECT * FROM nodes WHERE tree_id = ? AND node_id = ?
            UNION ALL
            SELECT * FROM nodes WHERE tree_id = ? AND path > ? AND path < ? AND depth <= ?
            ORDER BY path
        """,
            (tree_id, node_id, tree_id, root_path + "/", root_path + "/\x7f", root_depth + max_depth),
        )

        nodes = [Node(**dict(row)) for row in cursor.fetchall()]
        if not with_entities:
            return [n.to_dict() for n in nodes]

        entity_ids = list(dict.fromkeys(n.entity_id for n in nodes if n.entity_id))
        if not entity_ids:
            return [n.to_dict() for n in nodes]

        ent_map: dict[str, Entity] = {}
        for start in range(0, len(entity_ids), self._IN_QUERY_CHUNK_SIZE):
            chunk = entity_ids[start:start + self._IN_QUERY_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(f"SELECT * FROM entities WHERE entity_id IN ({placeholders})", chunk)
            ent_map.update(
                (row["entity_id"], Entity(**dict(row)))
                for row in cursor.fetchall()
            )

        result = []
        for n in nodes:
            d = n.to_dict()
            if n.entity_id and n.entity_id in ent_map:
                d["entity"] = ent_map[n.entity_id].to_dict()
            result.append(d)
        return result

    def get_entity(self, tree_id: str, node_id: str) -> Optional[Entity]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT e.*
            FROM nodes n
            JOIN entities e ON n.entity_id = e.entity_id
            WHERE n.tree_id = ? AND n.node_id = ?
        """,
            (tree_id, node_id),
        )
        ent_row = cursor.fetchone()
        return Entity(**dict(ent_row)) if ent_row else None

    def get_entities(self, tree_id: str, node_ids: list[str]) -> dict[str, Optional[Entity]]:
        """Fetch entities for several nodes, keyed by each unique requested node ID."""
        unique_ids = list(dict.fromkeys(node_ids))
        result: dict[str, Optional[Entity]] = {node_id: None for node_id in unique_ids}
        cursor = self.conn.cursor()

        for start in range(0, len(unique_ids), self._IN_QUERY_CHUNK_SIZE):
            chunk = unique_ids[start:start + self._IN_QUERY_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"""
                SELECT n.node_id AS source_node_id, e.*
                FROM nodes n
                JOIN entities e ON n.entity_id = e.entity_id
                WHERE n.tree_id = ? AND n.node_id IN ({placeholders})
                """,
                [tree_id, *chunk],
            )
            for row in cursor.fetchall():
                result[row["source_node_id"]] = Entity(
                    entity_id=row["entity_id"],
                    entity_type=row["entity_type"],
                    payload_json=row["payload_json"],
                    created_at=row["created_at"],
                )
        return result

    def get_root_id(self, tree_id: str) -> Optional[str]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT root_node_id FROM trees WHERE tree_id = ?", (tree_id,))
        row = cursor.fetchone()
        return row["root_node_id"] if row else None

    def ingest_tree(
        self,
        tree_structure: dict[str, Any],
        entities: Optional[dict[str, dict[str, Any]]] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> str:
        validate_json(meta)
        tree_id = str(uuid.uuid4())
        validate_json(tree_structure)
        validate_json(entities)
        root_id = tree_structure.get("node_id") or str(uuid.uuid4())
        now = self._ts()
        cursor = self.conn.cursor()

        with self.transaction():
            cursor.execute(
                "INSERT INTO trees (tree_id, root_node_id, created_at, updated_at, meta_json) VALUES (?, ?, ?, ?, ?)",
                (tree_id, root_id, now, now, json.dumps(meta) if meta else None),
            )

            for eid, payload in (entities or {}).items():
                encoded = json.dumps(payload, allow_nan=False)
                existing = cursor.execute("SELECT payload_json FROM entities WHERE entity_id = ?", (eid,)).fetchone()
                if existing:
                    if json.loads(existing[0]) != payload:
                        raise ValueError(f"conflicting entity identity: {eid}")
                else:
                    cursor.execute(
                        "INSERT INTO entities (entity_id, entity_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                        (eid, payload.get("type", "unknown"), encoded, now),
                    )

            def insert(
                data: dict, nid: str, pid: Optional[str], ppath: Optional[str], pdepth: int, slot: Optional[str], ordinal: int = 0
            ):
                depth = pdepth + 1 if pid else 0
                path = f"{ppath}/{ordinal:012d}" if ppath else "/r"
                ntype_str = data.get("type", "object")
                ntype = self.OBJECT if ntype_str == "object" else (self.ARRAY if ntype_str == "array" else self.LEAF)
                attrs = data.get("attrs")

                cursor.execute(
                    """
                    INSERT INTO nodes (tree_id, node_id, parent_id, slot, node_type, depth, path, entity_type, entity_id, attrs_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        tree_id,
                        nid,
                        pid,
                        slot,
                        ntype,
                        depth,
                        path,
                        data.get("entity_type"),
                        data.get("entity_id"),
                        json.dumps(attrs) if attrs else None,
                        now,
                        now,
                    ),
                )

                children = data.get("children")
                if children:
                    if isinstance(children, dict):
                        for i, (k, child) in enumerate(children.items()):
                            insert(child, child.get("node_id") or str(uuid.uuid4()), nid, path, depth, k, i)
                    elif isinstance(children, list):
                        for i, child in enumerate(children):
                            insert(child, child.get("node_id") or str(uuid.uuid4()), nid, path, depth, str(i), i)

            insert(tree_structure, root_id, None, None, -1, None)
            log.info(f"ingest_tree: {tree_id[:8]}")
            return tree_id

    def delete_tree(self, tree_id: str):
        cursor = self.conn.cursor()
        with self.transaction():
            cursor.execute(
                "SELECT DISTINCT entity_id FROM nodes WHERE tree_id = ? AND entity_id IS NOT NULL",
                (tree_id,),
            )
            entity_ids = [row["entity_id"] for row in cursor.fetchall()]

            cursor.execute("DELETE FROM nodes WHERE tree_id = ?", (tree_id,))
            cursor.execute("DELETE FROM trees WHERE tree_id = ?", (tree_id,))

            for i in range(0, len(entity_ids), 500):
                chunk = entity_ids[i:i + 500]
                placeholders = ",".join("?" for _ in chunk)
                cursor.execute(
                    f"""
                    DELETE FROM entities
                    WHERE entity_id IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1
                          FROM nodes
                          WHERE nodes.entity_id = entities.entity_id
                      )
                    """,
                    chunk,
                )

            log.info(f"delete_tree: {tree_id[:8]}")

    def check_integrity(self):
        result = [row[0] for row in self.conn.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            raise ValueError(f"ConDB integrity check failed: {result}")

    def checkpoint(self):
        """Flush committed WAL contents before closing and copying the main file."""
        with self._transaction_lock:
            if self.conn.in_transaction:
                raise RuntimeError("Cannot seal a database with an active transaction")
            if not self.read_only:
                busy, _, _ = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy:
                    raise RuntimeError("ConDB checkpoint blocked by an active connection")
            self.check_integrity()

    def close(self):
        if not self._closed:
            self.conn.close()
            self._closed = True
            log.debug(f"TreeDB closed: {self.db_path}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
