"""Stable user submissions, per-execution attempts and immutable answer metadata."""

CONVERSATION_SCHEMA = (
    """CREATE TABLE ukb_conversation_submissions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES ukb_conversations(session_id),
        submission_id TEXT NOT NULL,
        message_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        after_turn INTEGER NOT NULL CHECK (after_turn >= 0),
        submission_order INTEGER CHECK (submission_order >= 0),
        UNIQUE (session_id, submission_id)
    )""",
    "CREATE INDEX ukb_conversation_pending "
    "ON ukb_conversation_submissions("
    "session_id, after_turn, COALESCE(submission_order, sequence), sequence)",
    """CREATE TRIGGER ukb_submissions_immutable BEFORE UPDATE ON ukb_conversation_submissions
        BEGIN SELECT RAISE(ABORT, 'Conversation submissions are immutable'); END""",
    """CREATE TABLE ukb_conversation_runs (
        execution_id TEXT PRIMARY KEY REFERENCES ukb_executions(execution_id),
        session_id TEXT NOT NULL,
        submission_id TEXT NOT NULL,
        publication_id TEXT NOT NULL REFERENCES ukb_publications(publication_id),
        state TEXT NOT NULL CHECK (state IN
            ('started', 'completed', 'reused', 'failed', 'stopped', 'blocked', 'interrupted')),
        ordinal INTEGER,
        error_code TEXT,
        FOREIGN KEY (session_id, submission_id)
            REFERENCES ukb_conversation_submissions(session_id, submission_id),
        FOREIGN KEY (session_id, ordinal) REFERENCES ukb_turns(session_id, ordinal),
        CHECK ((state IN ('completed', 'reused')) = (ordinal IS NOT NULL))
    )""",
    "CREATE INDEX ukb_conversation_run_submission "
    "ON ukb_conversation_runs(session_id, submission_id, execution_id)",
    """CREATE TABLE ukb_turn_results (
        session_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        result_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        PRIMARY KEY (session_id, ordinal),
        FOREIGN KEY (session_id, ordinal) REFERENCES ukb_turns(session_id, ordinal)
    )""",
    """CREATE TRIGGER ukb_turn_results_immutable BEFORE UPDATE ON ukb_turn_results
        BEGIN SELECT RAISE(ABORT, 'Committed answer metadata is immutable'); END""",
    """CREATE TABLE ukb_conversation_answers (
        execution_id TEXT PRIMARY KEY REFERENCES ukb_conversation_runs(execution_id),
        turn_id TEXT NOT NULL,
        input_revision INTEGER NOT NULL CHECK (input_revision >= 0),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        payload_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        result_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        reused_from TEXT REFERENCES ukb_conversation_answers(execution_id)
    )""",
    """CREATE TRIGGER ukb_answers_immutable BEFORE UPDATE ON ukb_conversation_answers
        BEGIN SELECT RAISE(ABORT, 'Accepted conversation answers are immutable'); END""",
    *(
        f"""CREATE TRIGGER ukb_run_turn_{operation.lower()} BEFORE {operation}
        ON ukb_conversation_runs
        WHEN NEW.state IN ('completed', 'reused') AND NOT EXISTS (
            SELECT 1 FROM ukb_turns t JOIN ukb_turn_results r
            ON r.session_id = t.session_id AND r.ordinal = t.ordinal
            WHERE t.session_id = NEW.session_id AND t.ordinal = NEW.ordinal
            AND t.attempt_id = NEW.submission_id)
        BEGIN SELECT RAISE(ABORT, 'A completed run requires its exact turn and result'); END"""
        for operation in ("INSERT", "UPDATE")
    ),
    """CREATE TRIGGER ukb_run_receipt_immutable BEFORE UPDATE ON ukb_conversation_runs
        WHEN OLD.state IN ('completed', 'reused')
        BEGIN SELECT RAISE(ABORT, 'A completed conversation receipt is immutable'); END""",
)
