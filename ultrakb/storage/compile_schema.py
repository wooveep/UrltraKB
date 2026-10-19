"""Compiler delivery recovery records; accepted results retain their own roots."""

COMPILE_SCHEMA = (
    """CREATE TABLE ukb_compile_records (
        work_id TEXT NOT NULL REFERENCES ukb_compile_work(work_id),
        step_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        record_json TEXT NOT NULL CHECK (json_valid(record_json)),
        PRIMARY KEY (work_id, step_key)
    )""",
    """CREATE TABLE ukb_compile_responses (
        response_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL REFERENCES ukb_compile_work(work_id),
        step_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        content_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        metadata_digest TEXT NOT NULL REFERENCES ukb_blobs(digest)
    )""",
    "CREATE INDEX ukb_compile_response_work ON ukb_compile_responses(work_id, step_key)",
    """CREATE TABLE ukb_compile_invocations (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        invocation_id TEXT NOT NULL UNIQUE,
        work_id TEXT NOT NULL REFERENCES ukb_compile_work(work_id),
        step_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        stage TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('generation', 'repair', 'recovery', 'conflict'))
    )""",
    "CREATE INDEX ukb_compile_invocation_step "
    "ON ukb_compile_invocations(work_id, step_key, sequence DESC)",
    "CREATE INDEX ukb_compile_invocation_stage "
    "ON ukb_compile_invocations(work_id, execution_id, stage, sequence DESC)",
    "CREATE INDEX ukb_stage_work ON ukb_stage_attempts(work_key, stage_attempt_id)",
    "CREATE INDEX ukb_usage_stage_attempt ON ukb_usage_requests(stage_attempt_id, sequence)",
)
