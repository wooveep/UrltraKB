"""Physical records with concrete owners; schema creation is explicit."""

from contextdb.core.schema import SCHEMA as CONDB_SCHEMA

from .artifact_schema import ARTIFACT_SCHEMA
from .compile_schema import COMPILE_SCHEMA
from .conversation_schema import CONVERSATION_SCHEMA
from .execution_schema import EXECUTION_SCHEMA
from .generation_schema import GENERATION_SCHEMA
from .import_schema import IMPORT_CANDIDATE_SCHEMA
from .knowledge_inputs import PAGE_INPUT_SCHEMA
from .lint_schema import LINT_SCHEMA
from .projection_schema import PROJECTION_SCHEMA
from .publication_schema import PUBLICATION_SCHEMA
from .refresh_schema import REFRESH_SCHEMA
from .tree_schema import TREE_SCHEMA

SCHEMA = (
    (
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
        label TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'withdrawn')),
        generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
        contribution_empty INTEGER NOT NULL DEFAULT 0 CHECK (contribution_empty IN (0, 1))
    )""",
        """CREATE TABLE ukb_source_revisions (
        revision_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES ukb_sources(source_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        manifest_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        state TEXT NOT NULL CHECK (state IN ('draft', 'sealed', 'ready')),
        UNIQUE (source_id, ordinal)
    )""",
        "CREATE INDEX ukb_source_revisions_source "
        "ON ukb_source_revisions(source_id, state, ordinal DESC)",
        """CREATE TABLE ukb_processing_versions (
        processing_id TEXT PRIMARY KEY,
        source_revision_id TEXT NOT NULL REFERENCES ukb_source_revisions(revision_id),
        fingerprint TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        body_path TEXT NOT NULL,
        manifest_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        decision_json TEXT NOT NULL CHECK (json_valid(decision_json)),
        source_path TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK (source_kind IN ('markdown', 'text', 'pages')),
        content_state TEXT NOT NULL CHECK (content_state IN
            ('content', 'empty', 'withdrawn', 'objects_only', 'parse_failed', 'unreadable')),
        reprocessing_request_id TEXT REFERENCES ukb_reprocessing_requests(request_id),
        state TEXT NOT NULL CHECK (state IN ('draft', 'sealed', 'ready'))
    )""",
        "CREATE INDEX ukb_processing_source ON ukb_processing_versions(source_revision_id, state)",
        """CREATE TABLE ukb_workbook_inventories (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_id TEXT NOT NULL UNIQUE,
        source_revision_id TEXT NOT NULL REFERENCES ukb_source_revisions(revision_id),
        processing_key TEXT NOT NULL,
        complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
        payload_digest TEXT NOT NULL REFERENCES ukb_blobs(digest)
    )""",
        "CREATE INDEX ukb_workbook_attempts "
        "ON ukb_workbook_inventories(source_revision_id, processing_key, complete, sequence)",
        """CREATE TRIGGER ukb_workbook_inventory_immutable BEFORE UPDATE ON ukb_workbook_inventories
        BEGIN SELECT RAISE(ABORT, 'Worksheet inventory is immutable'); END""",
        """CREATE TABLE ukb_reprocessing_requests (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL REFERENCES ukb_sources(source_id),
        source_revision_id TEXT NOT NULL REFERENCES ukb_source_revisions(revision_id),
        payload_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        execution_id TEXT REFERENCES ukb_executions(execution_id),
        source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
        expected_source_generation INTEGER NOT NULL CHECK (
            expected_source_generation >= source_generation)
    )""",
        "CREATE INDEX ukb_reprocessing_source "
        "ON ukb_reprocessing_requests(source_revision_id, sequence DESC)",
        """CREATE TRIGGER ukb_reprocessing_immutable BEFORE UPDATE OF
        sequence, request_id, source_id, source_revision_id, payload_digest, source_generation
        ON ukb_reprocessing_requests
        BEGIN SELECT RAISE(ABORT, 'Reprocessing plans are immutable'); END""",
        """CREATE TRIGGER ukb_reprocessing_progress BEFORE UPDATE OF expected_source_generation
        ON ukb_reprocessing_requests
        WHEN NEW.expected_source_generation != OLD.expected_source_generation + 1
        BEGIN SELECT RAISE(ABORT, 'Reprocessing source progress must advance one target'); END""",
        """CREATE TABLE ukb_import_units (
        unit_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES ukb_sources(source_id),
        unit_key TEXT NOT NULL,
        doc_name TEXT NOT NULL UNIQUE,
        target_revision_id TEXT REFERENCES ukb_unit_revisions(unit_revision_id),
        generation INTEGER NOT NULL CHECK (generation >= 0),
        UNIQUE (source_id, unit_key)
    )""",
        """CREATE TABLE ukb_unit_revisions (
        unit_revision_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES ukb_import_units(unit_id),
        processing_id TEXT NOT NULL REFERENCES ukb_processing_versions(processing_id),
        generation INTEGER NOT NULL CHECK (generation > 0),
        UNIQUE (unit_id, generation)
    )""",
        """CREATE TABLE ukb_import_groups (
        root_import_id TEXT PRIMARY KEY,
        generation TEXT NOT NULL,
        budget_json TEXT NOT NULL CHECK (json_valid(budget_json)),
        cancelled INTEGER NOT NULL DEFAULT 0 CHECK (cancelled IN (0, 1))
    )""",
        """CREATE TABLE ukb_import_intents (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        intent_id TEXT NOT NULL UNIQUE,
        root_import_id TEXT NOT NULL REFERENCES ukb_import_groups(root_import_id),
        source_revision_id TEXT NOT NULL REFERENCES ukb_source_revisions(revision_id),
        unit_revision_id TEXT REFERENCES ukb_unit_revisions(unit_revision_id),
        work_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('import', 'discovery')),
        status TEXT NOT NULL CHECK (status IN
            ('pending', 'dispatched', 'started', 'completed', 'failed', 'blocked',
             'interrupted', 'stopped', 'cancelled', 'stale')),
        dispatch_id TEXT,
        attempt_id TEXT,
        UNIQUE (root_import_id, work_key)
    )""",
        "CREATE INDEX ukb_import_queue ON ukb_import_intents(status, sequence)",
        "CREATE INDEX ukb_import_group ON ukb_import_intents(root_import_id, sequence)",
        "CREATE INDEX ukb_import_unit_outcome "
        "ON ukb_import_intents(unit_revision_id, kind, sequence DESC)",
        "CREATE INDEX ukb_import_group_status "
        "ON ukb_import_intents(root_import_id, status, sequence)",
        """CREATE TABLE ukb_pending_attempts (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id TEXT NOT NULL UNIQUE,
        intent_id TEXT NOT NULL REFERENCES ukb_import_intents(intent_id),
        dispatch_id TEXT NOT NULL UNIQUE,
        generation TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('started', 'completed', 'failed', 'blocked', 'interrupted', 'stopped')),
        publication_id TEXT REFERENCES ukb_publications(publication_id),
        error_code TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    )""",
        "CREATE INDEX ukb_pending_attempt_intent ON ukb_pending_attempts(intent_id, sequence)",
        """CREATE TABLE ukb_conversations (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        view_id TEXT NOT NULL,
        model TEXT NOT NULL,
        language TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 0),
        turn_count INTEGER NOT NULL CHECK (turn_count >= 0),
        deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        activity_version INTEGER NOT NULL CHECK (activity_version >= 0)
    )""",
        "CREATE INDEX ukb_conversation_catalog "
        "ON ukb_conversations(view_id, deleted, updated_at DESC, sequence DESC)",
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
        UNIQUE (session_id, policy_key, checkpoint_id),
        UNIQUE (session_id, policy_key, prefix_digest)
    )""",
        """CREATE TABLE ukb_session_heads (
        session_id TEXT NOT NULL REFERENCES ukb_conversations(session_id),
        policy_key TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        PRIMARY KEY (session_id, policy_key),
        FOREIGN KEY (session_id, policy_key, checkpoint_id)
            REFERENCES ukb_conversation_checkpoints(session_id, policy_key, checkpoint_id)
    )""",
        """CREATE TABLE ukb_session_projections (
        session_id TEXT NOT NULL,
        policy_key TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        tree_id TEXT NOT NULL UNIQUE REFERENCES ukb_tree_builds(tree_id),
        input_fingerprint TEXT NOT NULL,
        PRIMARY KEY (session_id, policy_key),
        FOREIGN KEY (session_id, policy_key, checkpoint_id)
            REFERENCES ukb_conversation_checkpoints(session_id, policy_key, checkpoint_id)
    )""",
        "CREATE INDEX ukb_session_head_checkpoint ON ukb_session_heads(checkpoint_id)",
        "CREATE INDEX ukb_session_projection_checkpoint ON ukb_session_projections(checkpoint_id)",
        """CREATE TABLE ukb_knowledge_revisions (
        revision_id TEXT PRIMARY KEY,
        publication_id TEXT NOT NULL UNIQUE,
        view_id TEXT NOT NULL,
        expected_publication TEXT,
        manifest_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        navigation_fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('draft', 'sealed', 'ready', 'published', 'retiring'))
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
    + EXECUTION_SCHEMA
    + CONDB_SCHEMA
    + TREE_SCHEMA
    + PROJECTION_SCHEMA
    + PUBLICATION_SCHEMA
    + PAGE_INPUT_SCHEMA
    + CONVERSATION_SCHEMA
    + ARTIFACT_SCHEMA
    + COMPILE_SCHEMA
    + REFRESH_SCHEMA
    + LINT_SCHEMA
    + GENERATION_SCHEMA
    + IMPORT_CANDIDATE_SCHEMA
)
