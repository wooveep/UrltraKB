"""Physical records with concrete owners; schema creation is explicit."""

SCHEMA = (
    """CREATE TABLE ukb_blobs (
        digest TEXT PRIMARY KEY CHECK (length(digest) = 64 AND digest NOT GLOB '*[^a-f0-9]*'),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        state TEXT NOT NULL CHECK (state IN ('live', 'retiring'))
    )""",
    """CREATE TABLE ukb_blob_refs (
        owner_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        slot TEXT NOT NULL,
        digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        PRIMARY KEY (owner_kind, owner_id, slot)
    )""",
    "CREATE INDEX ukb_blob_refs_digest ON ukb_blob_refs(digest)",
    """CREATE TRIGGER ukb_blob_refs_live_insert BEFORE INSERT ON ukb_blob_refs
        WHEN NOT EXISTS (SELECT 1 FROM ukb_blobs WHERE digest = NEW.digest AND state = 'live')
        BEGIN SELECT RAISE(ABORT, 'Blob is not available for new references'); END""",
    """CREATE TRIGGER ukb_blob_refs_live_update BEFORE UPDATE OF digest ON ukb_blob_refs
        WHEN NOT EXISTS (SELECT 1 FROM ukb_blobs WHERE digest = NEW.digest AND state = 'live')
        BEGIN SELECT RAISE(ABORT, 'Blob is not available for new references'); END""",
    """CREATE TABLE ukb_sources (
        source_id TEXT PRIMARY KEY,
        source_key TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL
    )""",
    """CREATE TABLE ukb_source_revisions (
        revision_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES ukb_sources(source_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        manifest_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        UNIQUE (source_id, ordinal)
    )""",
    "CREATE INDEX ukb_source_revisions_source ON ukb_source_revisions(source_id, ordinal DESC)",
    """CREATE TABLE ukb_conversations (
        session_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        view_id TEXT NOT NULL,
        model TEXT NOT NULL,
        language TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        deleted INTEGER NOT NULL CHECK (deleted IN (0, 1))
    )""",
    """CREATE TABLE ukb_turns (
        session_id TEXT NOT NULL REFERENCES ukb_conversations(session_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        turn_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        payload_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        PRIMARY KEY (session_id, ordinal),
        UNIQUE (session_id, turn_id),
        UNIQUE (session_id, attempt_id)
    )""",
    """CREATE TRIGGER ukb_turns_immutable BEFORE UPDATE ON ukb_turns
        BEGIN SELECT RAISE(ABORT, 'Committed conversation turns are immutable'); END""",
    """CREATE TABLE ukb_conversation_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES ukb_conversations(session_id),
        policy_key TEXT NOT NULL,
        turn_count INTEGER NOT NULL CHECK (turn_count >= 0),
        prefix_digest TEXT NOT NULL,
        state_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        UNIQUE (session_id, policy_key, prefix_digest)
    )""",
    """CREATE TABLE ukb_session_heads (
        session_id TEXT NOT NULL REFERENCES ukb_conversations(session_id),
        policy_key TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL REFERENCES ukb_conversation_checkpoints(checkpoint_id),
        PRIMARY KEY (session_id, policy_key)
    )""",
    """CREATE TABLE ukb_knowledge_revisions (
        revision_id TEXT PRIMARY KEY,
        publication_id TEXT NOT NULL UNIQUE,
        view_id TEXT NOT NULL,
        expected_publication TEXT,
        manifest_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        state TEXT NOT NULL CHECK (state IN ('draft', 'sealed', 'ready', 'published'))
    )""",
    """CREATE TABLE ukb_revision_files (
        revision_id TEXT NOT NULL REFERENCES ukb_knowledge_revisions(revision_id),
        path TEXT NOT NULL,
        digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        PRIMARY KEY (revision_id, path)
    )""",
    """CREATE TRIGGER ukb_revision_files_draft BEFORE INSERT ON ukb_revision_files
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = NEW.revision_id AND state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Sealed revisions are immutable'); END""",
    """CREATE TRIGGER ukb_revision_files_no_update BEFORE UPDATE ON ukb_revision_files
        BEGIN SELECT RAISE(ABORT, 'Revision files are immutable'); END""",
    """CREATE TABLE ukb_publications (
        publication_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL UNIQUE REFERENCES ukb_knowledge_revisions(revision_id),
        view_id TEXT NOT NULL,
        generation TEXT NOT NULL
    )""",
    """CREATE TABLE ukb_heads (
        view_id TEXT PRIMARY KEY,
        publication_id TEXT NOT NULL REFERENCES ukb_publications(publication_id)
    )""",
)
