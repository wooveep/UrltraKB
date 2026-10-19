"""Independent artifact acceptance, a fixed output epoch and per-file projection."""

ARTIFACT_SCHEMA = (
    "CREATE TABLE ukb_artifact_state (slot INTEGER PRIMARY KEY CHECK (slot = 1), "
    "epoch INTEGER NOT NULL CHECK (epoch >= 0))",
    "INSERT INTO ukb_artifact_state VALUES (1, 0)",
    """CREATE TABLE ukb_artifact_revisions (
        revision_id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        evidence_publication_id TEXT NOT NULL REFERENCES ukb_publications(publication_id),
        expected_epoch INTEGER NOT NULL CHECK (expected_epoch >= 0),
        state TEXT NOT NULL CHECK (state IN ('accepted', 'conflict')),
        epoch INTEGER UNIQUE CHECK (epoch > 0),
        accepted_at TEXT NOT NULL,
        operation TEXT NOT NULL CHECK (operation IN ('write', 'delete')),
        CHECK ((state = 'accepted') = (epoch IS NOT NULL))
    )""",
    "CREATE INDEX ukb_artifact_history ON ukb_artifact_revisions(path, epoch DESC) "
    "WHERE state = 'accepted'",
    "CREATE INDEX ukb_artifact_execution ON ukb_artifact_revisions(execution_id)",
    """CREATE TRIGGER ukb_artifact_immutable BEFORE UPDATE ON ukb_artifact_revisions
        BEGIN SELECT RAISE(ABORT, 'Artifact revisions are immutable'); END""",
    """CREATE TABLE ukb_artifact_heads (
        path TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL UNIQUE REFERENCES ukb_artifact_revisions(revision_id),
        applied_digest TEXT REFERENCES ukb_blobs(digest),
        pending_revision_id TEXT REFERENCES ukb_artifact_revisions(revision_id),
        pending_base_digest TEXT REFERENCES ukb_blobs(digest),
        CHECK (pending_revision_id IS NOT NULL OR pending_base_digest IS NULL)
    )""",
)
