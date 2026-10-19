"""Immutable compiled import candidates with independently owned review metadata."""

IMPORT_CANDIDATE_SCHEMA = (
    """CREATE TABLE ukb_import_candidates (
        proposal_id TEXT PRIMARY KEY REFERENCES ukb_knowledge_revisions(revision_id),
        detail_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        version TEXT NOT NULL CHECK (length(version) = 64),
        conflict_count INTEGER NOT NULL CHECK (conflict_count >= 0),
        original_id TEXT NOT NULL REFERENCES ukb_import_candidates(proposal_id)
    )""",
    "CREATE INDEX ukb_import_candidate_execution ON ukb_import_candidates(execution_id)",
    "CREATE INDEX ukb_import_candidate_original ON ukb_import_candidates(original_id)",
    """CREATE TRIGGER ukb_import_candidate_immutable BEFORE UPDATE ON ukb_import_candidates
        BEGIN SELECT RAISE(ABORT, 'Import candidates are immutable'); END""",
    """CREATE TRIGGER ukb_import_candidate_retained BEFORE DELETE ON ukb_import_candidates
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.proposal_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release import candidates'); END""",
)
