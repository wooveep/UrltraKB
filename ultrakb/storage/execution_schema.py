"""Task, model-budget, and receipt records with independent commit lifetimes."""

TOKENS = ("input_total", "output_total", "cached_input", "reasoning_output")
COUNTS = (
    *TOKENS,
    *(f"missing_{key}" for key in TOKENS),
    "requests",
    "actual_sends",
    "unsent_attempts",
    "send_unknown",
    "unknown_requests",
    "in_flight_requests",
    "collection_pending",
    "usage_pending",
)

EXECUTION_SCHEMA = (
    """CREATE TABLE ukb_tasks (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL,
        created_at TEXT NOT NULL,
        hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1))
    )""",
    """CREATE TABLE ukb_model_budgets (
        model_budget_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES ukb_tasks(task_id),
        fingerprint TEXT NOT NULL,
        max_calls INTEGER CHECK (max_calls >= 0),
        used INTEGER NOT NULL CHECK (used >= 0),
        deadline_at REAL
    )""",
    """CREATE TABLE ukb_application_budget_bindings (
        application_task_id TEXT NOT NULL,
        unit_ordinal TEXT NOT NULL,
        model_budget_id TEXT NOT NULL REFERENCES ukb_model_budgets(model_budget_id),
        PRIMARY KEY (application_task_id, unit_ordinal)
    )""",
    """CREATE TABLE ukb_executions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        execution_id TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL REFERENCES ukb_tasks(task_id),
        model_budget_id TEXT NOT NULL REFERENCES ukb_model_budgets(model_budget_id),
        generation TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        pending_attempt_id TEXT REFERENCES ukb_pending_attempts(attempt_id),
        application_task_id TEXT,
        application_unit_id TEXT,
        parent_application_task_id TEXT,
        process_clock_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        started_monotonic REAL NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('started', 'stopping', 'completed', 'failed', 'cancelled', 'interrupted')),
        ended_at TEXT,
        duration_seconds REAL CHECK (duration_seconds >= 0),
        CHECK (application_task_id IS NOT NULL OR
            (application_unit_id IS NULL AND parent_application_task_id IS NULL))
    )""",
    "CREATE INDEX ukb_executions_task ON ukb_executions(task_id, sequence)",
    "CREATE INDEX ukb_executions_budget ON ukb_executions(model_budget_id, sequence)",
    "CREATE UNIQUE INDEX ukb_one_active_execution_per_task ON ukb_executions(task_id) "
    "WHERE state IN ('started', 'stopping')",
    """CREATE TABLE ukb_stage_attempts (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        stage_attempt_id TEXT NOT NULL UNIQUE,
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        parent_stage_id TEXT REFERENCES ukb_stage_attempts(stage_attempt_id),
        stage_code TEXT NOT NULL,
        work_key TEXT,
        ordinal INTEGER CHECK (ordinal >= 0),
        source_revision_id TEXT REFERENCES ukb_source_revisions(revision_id),
        unit_revision_id TEXT REFERENCES ukb_unit_revisions(unit_revision_id),
        process_clock_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        started_monotonic REAL NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('started', 'completed', 'failed', 'cancelled', 'interrupted', 'reused')),
        ended_at TEXT,
        duration_seconds REAL CHECK (duration_seconds >= 0),
        error_code TEXT
    )""",
    "CREATE INDEX ukb_stage_execution ON ukb_stage_attempts(execution_id, sequence)",
    "CREATE INDEX ukb_stage_active ON ukb_stage_attempts(execution_id, state)",
    "CREATE INDEX ukb_stage_source ON ukb_stage_attempts(source_revision_id, sequence DESC)",
    """CREATE TABLE ukb_usage_requests (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL UNIQUE,
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        model_budget_id TEXT NOT NULL REFERENCES ukb_model_budgets(model_budget_id),
        logical_call_id TEXT,
        budget_charged INTEGER NOT NULL DEFAULT 1 CHECK (budget_charged IN (0, 1)),
        source_id TEXT REFERENCES ukb_sources(source_id),
        unit_id TEXT REFERENCES ukb_import_units(unit_id),
        root_import_id TEXT REFERENCES ukb_import_groups(root_import_id),
        stage_code TEXT NOT NULL,
        stage_attempt_id TEXT REFERENCES ukb_stage_attempts(stage_attempt_id),
        storage_version INTEGER NOT NULL CHECK (storage_version >= 1),
        observation_version INTEGER NOT NULL CHECK (observation_version >= 0),
        intent_digest TEXT NOT NULL,
        field_versions_json TEXT NOT NULL CHECK (json_valid(field_versions_json)),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
    )""",
    "CREATE INDEX ukb_usage_execution ON ukb_usage_requests(execution_id, sequence)",
    "CREATE INDEX ukb_usage_source ON ukb_usage_requests(source_id, sequence)",
    "CREATE INDEX ukb_usage_unit ON ukb_usage_requests(unit_id, sequence)",
    "CREATE INDEX ukb_usage_logical ON ukb_usage_requests(logical_call_id, sequence)",
    """CREATE TABLE ukb_response_receipts (
        request_id TEXT PRIMARY KEY REFERENCES ukb_usage_requests(request_id),
        response_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        state TEXT NOT NULL CHECK (state IN ('received', 'accepted', 'rejected')),
        error_code TEXT
    )""",
    """CREATE TABLE ukb_compile_work (
        work_id TEXT PRIMARY KEY,
        model_budget_id TEXT NOT NULL REFERENCES ukb_model_budgets(model_budget_id),
        object_digest TEXT NOT NULL,
        conditions_digest TEXT NOT NULL,
        object_json TEXT NOT NULL CHECK (json_valid(object_json)),
        conditions_json TEXT NOT NULL CHECK (json_valid(conditions_json)),
        UNIQUE (model_budget_id, object_digest, conditions_digest)
    )""",
    """CREATE TABLE ukb_compile_steps (
        work_id TEXT NOT NULL REFERENCES ukb_compile_work(work_id),
        step_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        result_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
        PRIMARY KEY (work_id, step_key)
    )""",
    """CREATE TABLE ukb_compile_page_bases (
        model_budget_id TEXT NOT NULL REFERENCES ukb_model_budgets(model_budget_id),
        object_digest TEXT NOT NULL,
        page_path TEXT NOT NULL,
        base_digest TEXT NOT NULL,
        PRIMARY KEY (model_budget_id, object_digest, page_path)
    )""",
    "CREATE TABLE ukb_usage_counters (bucket TEXT NOT NULL, identity TEXT NOT NULL, "
    "stage_code TEXT NOT NULL, "
    + ", ".join(f"{key} INTEGER NOT NULL CHECK ({key} >= 0)" for key in COUNTS)
    + ", PRIMARY KEY(bucket, identity, stage_code))",
)
