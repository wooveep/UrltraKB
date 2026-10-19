"""Retained lint candidates join repair/report outcomes to exact publication receipts."""

LINT_SCHEMA = (
    """CREATE TABLE ukb_lint_candidates (
        revision_id TEXT PRIMARY KEY REFERENCES ukb_knowledge_revisions(revision_id),
        work_key TEXT NOT NULL,
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        detail_digest TEXT NOT NULL REFERENCES ukb_blobs(digest)
    )""",
    "CREATE INDEX ukb_lint_work ON ukb_lint_candidates(work_key)",
    """CREATE TRIGGER ukb_lint_ready BEFORE INSERT ON ukb_lint_candidates
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = NEW.revision_id AND state = 'ready')
        BEGIN SELECT RAISE(ABORT, 'Lint requires its exact ready candidate'); END""",
    """CREATE TRIGGER ukb_lint_immutable BEFORE UPDATE ON ukb_lint_candidates
        BEGIN SELECT RAISE(ABORT, 'Lint candidates are immutable'); END""",
    """CREATE TRIGGER ukb_lint_retained BEFORE DELETE ON ukb_lint_candidates
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.revision_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release lint candidates'); END""",
)
