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
        UNIQUE (index_revision_id, unit_revision_id),
        UNIQUE (unit_revision_id, content_fingerprint)
    )""",
    """CREATE TABLE ukb_revision_inputs (
        revision_id TEXT NOT NULL REFERENCES ukb_knowledge_revisions(revision_id),
        unit_revision_id TEXT NOT NULL REFERENCES ukb_unit_revisions(unit_revision_id),
        index_revision_id TEXT,
        PRIMARY KEY (revision_id, unit_revision_id),
        FOREIGN KEY (index_revision_id, unit_revision_id)
            REFERENCES ukb_index_revisions(index_revision_id, unit_revision_id)
    )""",
    """CREATE TABLE ukb_revision_navigation (
        revision_id TEXT PRIMARY KEY REFERENCES ukb_knowledge_revisions(revision_id),
        tree_id TEXT NOT NULL REFERENCES ukb_tree_builds(tree_id),
        input_fingerprint TEXT NOT NULL
    )""",
    """CREATE TRIGGER ukb_navigation_identity BEFORE UPDATE ON ukb_revision_navigation
        WHEN NEW.revision_id != OLD.revision_id OR NEW.input_fingerprint != OLD.input_fingerprint
        BEGIN SELECT RAISE(ABORT, 'Navigation input identity is immutable'); END""",
    *(
        f"""CREATE TRIGGER ukb_navigation_ready_{operation.lower()}
            BEFORE {operation} ON ukb_revision_navigation
            WHEN NOT EXISTS (
                SELECT 1 FROM ukb_knowledge_revisions r JOIN ukb_tree_builds t
                ON t.tree_id = NEW.tree_id AND t.input_fingerprint = NEW.input_fingerprint
                WHERE r.revision_id = NEW.revision_id
                AND r.navigation_fingerprint = NEW.input_fingerprint
                AND t.protocol = 'generic' AND t.state = 'ready')
            BEGIN SELECT RAISE(ABORT, 'Navigation must match its ready tree and revision'); END"""
        for operation in ("INSERT", "UPDATE")
    ),
    """CREATE TRIGGER ukb_revision_inputs_draft BEFORE INSERT ON ukb_revision_inputs
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = NEW.revision_id AND state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Sealed revision inputs are immutable'); END""",
    """CREATE TRIGGER ukb_revision_inputs_no_update BEFORE UPDATE ON ukb_revision_inputs
        BEGIN SELECT RAISE(ABORT, 'Revision input bindings are immutable'); END""",
    """CREATE TRIGGER ukb_revision_inputs_no_delete BEFORE DELETE ON ukb_revision_inputs
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
