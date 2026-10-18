"""ConDB owns its schema identity independently of the host database version."""

SCHEMA_VERSION = 2
SCHEMA = (
    "CREATE TABLE condb_schema (version INTEGER NOT NULL)",
    f"INSERT INTO condb_schema VALUES ({SCHEMA_VERSION})",
    """CREATE TABLE trees (
        tree_id TEXT PRIMARY KEY,
        root_node_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        meta_json TEXT
    )""",
    """CREATE TABLE entities (
        tree_id TEXT NOT NULL REFERENCES trees(tree_id),
        entity_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (tree_id, entity_id)
    )""",
    """CREATE TABLE nodes (
        tree_id TEXT NOT NULL REFERENCES trees(tree_id),
        node_id TEXT NOT NULL,
        parent_id TEXT,
        slot TEXT,
        node_type INTEGER NOT NULL,
        depth INTEGER NOT NULL,
        path TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        attrs_json TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (tree_id, node_id),
        FOREIGN KEY (tree_id, parent_id) REFERENCES nodes(tree_id, node_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (tree_id, entity_id) REFERENCES entities(tree_id, entity_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    "CREATE UNIQUE INDEX nodes_child_unique ON nodes(tree_id, parent_id, slot)",
    "CREATE INDEX nodes_path_idx ON nodes(tree_id, path)",
    "CREATE INDEX nodes_parent_idx ON nodes(tree_id, parent_id)",
    "CREATE INDEX nodes_entity_idx ON nodes(tree_id, entity_type, entity_id)",
    "CREATE INDEX nodes_retire_idx ON nodes(tree_id, depth DESC, node_id)",
)
