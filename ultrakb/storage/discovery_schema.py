"""Frozen discovery inputs, atomic object acceptance and source-import receipts."""

DISCOVERY_SCHEMA = (
    """CREATE TABLE ukb_discovery_groups (
        root_import_id TEXT PRIMARY KEY REFERENCES ukb_import_groups(root_import_id),
        sources INTEGER NOT NULL DEFAULT 1 CHECK (sources >= 1),
        object_bytes INTEGER NOT NULL DEFAULT 0 CHECK (object_bytes >= 0),
        decompressed_bytes INTEGER NOT NULL DEFAULT 0 CHECK (decompressed_bytes >= 0),
        discovery_seconds REAL NOT NULL DEFAULT 0 CHECK (discovery_seconds >= 0),
        budget_origin TEXT NOT NULL DEFAULT 'default'
    )""",
    """CREATE TABLE ukb_discovery_runs (
        intent_id TEXT PRIMARY KEY REFERENCES ukb_import_intents(intent_id),
        source_revision_id TEXT NOT NULL REFERENCES ukb_source_revisions(revision_id),
        policy TEXT NOT NULL,
        processing_key TEXT NOT NULL,
        metadata_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        cursor INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),
        UNIQUE (source_revision_id, policy, processing_key)
    )""",
    """CREATE TRIGGER ukb_discovery_identity BEFORE UPDATE OF intent_id,
        source_revision_id, policy, processing_key, metadata_digest ON ukb_discovery_runs
        BEGIN SELECT RAISE(ABORT, 'Discovery input identity is immutable'); END""",
    """CREATE TABLE ukb_discovery_objects (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        checkpoint_id TEXT NOT NULL UNIQUE,
        discovery_intent_id TEXT NOT NULL REFERENCES ukb_discovery_runs(intent_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        object_key TEXT NOT NULL,
        payload_digest TEXT REFERENCES ukb_blobs(digest),
        import_intent_id TEXT UNIQUE REFERENCES ukb_import_intents(intent_id),
        filename TEXT,
        outcome TEXT NOT NULL,
        diagnostic TEXT,
        UNIQUE (discovery_intent_id, ordinal),
        UNIQUE (discovery_intent_id, object_key),
        CHECK ((payload_digest IS NULL) = (import_intent_id IS NULL)),
        CHECK ((payload_digest IS NULL) = (filename IS NULL))
    )""",
    "CREATE INDEX ukb_discovery_objects_run ON ukb_discovery_objects(discovery_intent_id, ordinal)",
    """CREATE TRIGGER ukb_discovery_object_immutable BEFORE UPDATE ON ukb_discovery_objects
        BEGIN SELECT RAISE(ABORT, 'Accepted discovery objects are immutable'); END""",
    """CREATE TABLE ukb_pending_source_bindings (
        intent_id TEXT PRIMARY KEY REFERENCES ukb_import_intents(intent_id),
        source_revision_id TEXT NOT NULL REFERENCES ukb_source_revisions(revision_id),
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id)
    )""",
    """CREATE TRIGGER ukb_pending_source_immutable BEFORE UPDATE ON ukb_pending_source_bindings
        BEGIN SELECT RAISE(ABORT, 'Pending imports bind one admitted original'); END""",
    """CREATE TABLE ukb_pending_import_results (
        attempt_id TEXT PRIMARY KEY REFERENCES ukb_pending_attempts(attempt_id),
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        payload_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        status TEXT NOT NULL,
        summary_json TEXT NOT NULL CHECK (json_valid(summary_json))
    )""",
    """CREATE TRIGGER ukb_pending_result_immutable BEFORE UPDATE ON ukb_pending_import_results
        BEGIN SELECT RAISE(ABORT, 'Import attempt results are immutable'); END""",
    """CREATE TRIGGER ukb_source_import_success BEFORE UPDATE ON ukb_pending_attempts
        WHEN NEW.status IN ('completed', 'not_imported')
        AND EXISTS (SELECT 1 FROM ukb_import_intents i WHERE i.intent_id = NEW.intent_id
                    AND i.kind = 'import' AND i.unit_revision_id IS NULL)
        AND NOT EXISTS (SELECT 1 FROM ukb_pending_import_results r
                        WHERE r.attempt_id = NEW.attempt_id AND r.status = NEW.status)
        BEGIN SELECT RAISE(ABORT, 'Source import requires its exact delivered result'); END""",
    """CREATE UNIQUE INDEX ukb_active_pending_execution ON ukb_executions(pending_attempt_id)
        WHERE pending_attempt_id IS NOT NULL AND state IN ('started', 'stopping')""",
)
