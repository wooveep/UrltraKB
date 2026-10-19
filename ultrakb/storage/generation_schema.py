"""Generator plans retain exact output bytes before independently accepting files."""

GENERATION_SCHEMA = (
    """CREATE TABLE ukb_generation_plans (
        plan_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL REFERENCES ukb_compile_work(work_id),
        request_key TEXT NOT NULL,
        model_budget_id TEXT NOT NULL REFERENCES ukb_model_budgets(model_budget_id),
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        publication_id TEXT NOT NULL REFERENCES ukb_publications(publication_id),
        expected_epoch INTEGER NOT NULL CHECK (expected_epoch >= 0),
        detail_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        file_count INTEGER NOT NULL CHECK (file_count >= 0),
        manifest_digest TEXT NOT NULL,
        knowledge_revision_id TEXT REFERENCES ukb_knowledge_revisions(revision_id),
        state TEXT NOT NULL CHECK (state IN ('building', 'ready', 'retiring'))
    )""",
    "CREATE INDEX ukb_generation_request ON ukb_generation_plans(model_budget_id, request_key)",
    "CREATE INDEX ukb_generation_revision ON ukb_generation_plans(knowledge_revision_id) "
    "WHERE knowledge_revision_id IS NOT NULL",
    """CREATE TABLE ukb_generation_files (
        plan_id TEXT NOT NULL REFERENCES ukb_generation_plans(plan_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        path TEXT NOT NULL,
        revision_id TEXT NOT NULL UNIQUE,
        digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        operation TEXT NOT NULL CHECK (operation IN ('write', 'delete')),
        PRIMARY KEY (plan_id, ordinal),
        UNIQUE (plan_id, path)
    )""",
    """CREATE TRIGGER ukb_generation_file_building BEFORE INSERT ON ukb_generation_files
        WHEN NOT EXISTS (SELECT 1 FROM ukb_generation_plans
                         WHERE plan_id = NEW.plan_id AND state = 'building')
        BEGIN SELECT RAISE(ABORT, 'Generator files require a building plan'); END""",
    """CREATE TRIGGER ukb_generation_file_immutable BEFORE UPDATE ON ukb_generation_files
        BEGIN SELECT RAISE(ABORT, 'Generator files are immutable'); END""",
    """CREATE TRIGGER ukb_generation_file_retained BEFORE DELETE ON ukb_generation_files
        WHEN NOT EXISTS (SELECT 1 FROM ukb_generation_plans
                         WHERE plan_id = OLD.plan_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring plans release generator files'); END""",
    """CREATE TRIGGER ukb_generation_seal BEFORE UPDATE ON ukb_generation_plans
        WHEN NOT (
            NEW.plan_id = OLD.plan_id AND NEW.work_id = OLD.work_id
            AND NEW.request_key = OLD.request_key AND NEW.model_budget_id = OLD.model_budget_id
            AND NEW.execution_id = OLD.execution_id AND NEW.publication_id = OLD.publication_id
            AND NEW.expected_epoch = OLD.expected_epoch AND NEW.detail_digest = OLD.detail_digest
            AND NEW.file_count = OLD.file_count AND NEW.manifest_digest = OLD.manifest_digest
            AND NEW.knowledge_revision_id IS OLD.knowledge_revision_id
            AND ((OLD.state = 'building' AND NEW.state = 'ready')
                 OR (OLD.state IN ('building', 'ready') AND NEW.state = 'retiring'))
        )
        BEGIN SELECT RAISE(ABORT, 'Generator plan identity is immutable'); END""",
    """CREATE TRIGGER ukb_generation_retained BEFORE DELETE ON ukb_generation_plans
        WHEN OLD.state != 'retiring'
        BEGIN SELECT RAISE(ABORT, 'Generator plans require explicit retirement'); END""",
)
