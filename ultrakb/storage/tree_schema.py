"""Publication-independent sealed tree builds and concrete business references."""

TREE_SCHEMA = (
    """CREATE TABLE ukb_tree_builds (
        tree_id TEXT PRIMARY KEY REFERENCES trees(tree_id),
        protocol TEXT NOT NULL CHECK (protocol IN ('document', 'chatindex', 'generic')),
        input_fingerprint TEXT NOT NULL,
        build_generation TEXT NOT NULL,
        manifest_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        node_count INTEGER NOT NULL CHECK (node_count > 0),
        entity_count INTEGER NOT NULL CHECK (entity_count >= 0),
        state TEXT NOT NULL CHECK (state IN ('draft', 'sealed', 'ready', 'retiring'))
    )""",
    "CREATE INDEX ukb_tree_state ON ukb_tree_builds(state, tree_id)",
    """CREATE TABLE ukb_tree_refs (
        owner_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        slot TEXT NOT NULL,
        tree_id TEXT NOT NULL REFERENCES ukb_tree_builds(tree_id),
        PRIMARY KEY (owner_kind, owner_id, slot)
    )""",
    "CREATE INDEX ukb_tree_refs_tree ON ukb_tree_refs(tree_id)",
    """CREATE TABLE ukb_index_revisions (
        index_revision_id TEXT PRIMARY KEY,
        unit_revision_id TEXT NOT NULL REFERENCES ukb_unit_revisions(unit_revision_id),
        tree_id TEXT NOT NULL UNIQUE REFERENCES ukb_tree_builds(tree_id),
        content_fingerprint TEXT NOT NULL,
        UNIQUE (unit_revision_id, content_fingerprint)
    )""",
    """CREATE TABLE ukb_revision_indexes (
        revision_id TEXT NOT NULL REFERENCES ukb_knowledge_revisions(revision_id),
        index_revision_id TEXT NOT NULL REFERENCES ukb_index_revisions(index_revision_id),
        PRIMARY KEY (revision_id, index_revision_id)
    )""",
    """CREATE TRIGGER ukb_revision_indexes_draft BEFORE INSERT ON ukb_revision_indexes
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = NEW.revision_id AND state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Sealed revision inputs are immutable'); END""",
    """CREATE TRIGGER ukb_revision_indexes_no_update BEFORE UPDATE ON ukb_revision_indexes
        BEGIN SELECT RAISE(ABORT, 'Revision input bindings are immutable'); END""",
    """CREATE TRIGGER ukb_revision_indexes_no_delete BEFORE DELETE ON ukb_revision_indexes
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.revision_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Revision inputs must retire through reclamation'); END""",
    """CREATE TRIGGER ukb_tree_ref_ready BEFORE INSERT ON ukb_tree_refs
        WHEN NOT EXISTS (SELECT 1 FROM ukb_tree_builds
                         WHERE tree_id = NEW.tree_id AND state = 'ready')
        BEGIN SELECT RAISE(ABORT, 'Tree is not ready for a business reference'); END""",
    """CREATE TRIGGER ukb_tree_ref_update BEFORE UPDATE OF tree_id ON ukb_tree_refs
        WHEN NOT EXISTS (SELECT 1 FROM ukb_tree_builds
                         WHERE tree_id = NEW.tree_id AND state = 'ready')
        BEGIN SELECT RAISE(ABORT, 'Tree is not ready for a business reference'); END""",
    *(
        f"""CREATE TRIGGER ukb_{table}_insert_draft BEFORE INSERT ON {table}
            WHEN NOT EXISTS (SELECT 1 FROM ukb_tree_builds
                             WHERE tree_id = NEW.tree_id AND state = 'draft')
            BEGIN SELECT RAISE(ABORT, 'Only draft tree rows can be appended'); END"""
        for table in ("nodes", "entities")
    ),
    *(
        f"""CREATE TRIGGER ukb_{table}_update_draft BEFORE UPDATE ON {table}
            WHEN NEW.tree_id != OLD.tree_id OR NOT EXISTS (SELECT 1 FROM ukb_tree_builds
                             WHERE tree_id = OLD.tree_id AND state = 'draft')
            BEGIN SELECT RAISE(ABORT, 'Sealed tree rows are immutable'); END"""
        for table in ("trees", "nodes", "entities")
    ),
    *(
        f"""CREATE TRIGGER ukb_{table}_delete_retiring BEFORE DELETE ON {table}
            WHEN NOT EXISTS (SELECT 1 FROM ukb_tree_builds
                             WHERE tree_id = OLD.tree_id AND state = 'retiring')
            BEGIN SELECT RAISE(ABORT, 'Tree rows must retire through reclamation'); END"""
        for table in ("nodes", "entities")
    ),
)
