"""Catalog fences, generated-page baselines and durable refresh review ownership."""

REFRESH_SCHEMA = (
    "CREATE TABLE ukb_catalog_state (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
    "generation INTEGER NOT NULL CHECK (generation >= 0))",
    "INSERT INTO ukb_catalog_state VALUES (1, 0)",
    """CREATE TRIGGER ukb_catalog_source_insert AFTER INSERT ON ukb_sources
        BEGIN UPDATE ukb_catalog_state SET generation = generation + 1 WHERE singleton = 1; END""",
    """CREATE TRIGGER ukb_catalog_source_change AFTER UPDATE OF state, generation ON ukb_sources
        WHEN NEW.state != OLD.state OR NEW.generation != OLD.generation
        BEGIN UPDATE ukb_catalog_state SET generation = generation + 1 WHERE singleton = 1; END""",
    """CREATE TRIGGER ukb_catalog_unit_insert AFTER INSERT ON ukb_import_units
        BEGIN UPDATE ukb_catalog_state SET generation = generation + 1 WHERE singleton = 1; END""",
    """CREATE TRIGGER ukb_catalog_unit_change AFTER UPDATE OF target_revision_id ON ukb_import_units
        WHEN NEW.target_revision_id IS NOT OLD.target_revision_id
        BEGIN UPDATE ukb_catalog_state SET generation = generation + 1 WHERE singleton = 1; END""",
    """CREATE TABLE ukb_revision_generated (
        revision_id TEXT NOT NULL REFERENCES ukb_knowledge_revisions(revision_id),
        path TEXT NOT NULL,
        digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        PRIMARY KEY (revision_id, path)
    )""",
    """CREATE TRIGGER ukb_generated_draft BEFORE INSERT ON ukb_revision_generated
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = NEW.revision_id AND state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Generated baselines require a draft revision'); END""",
    """CREATE TRIGGER ukb_generated_immutable BEFORE UPDATE ON ukb_revision_generated
        BEGIN SELECT RAISE(ABORT, 'Generated baselines are immutable'); END""",
    """CREATE TRIGGER ukb_generated_retained BEFORE DELETE ON ukb_revision_generated
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.revision_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release generated baselines'); END""",
    """CREATE TABLE ukb_revision_refresh (
        revision_id TEXT PRIMARY KEY REFERENCES ukb_knowledge_revisions(revision_id),
        catalog_generation INTEGER NOT NULL CHECK (catalog_generation >= 0),
        outcome_json TEXT NOT NULL CHECK (
            json_valid(outcome_json) AND json_type(outcome_json) = 'object')
    )""",
    """CREATE TRIGGER ukb_refresh_draft BEFORE INSERT ON ukb_revision_refresh
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = NEW.revision_id AND state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Refresh claims require a draft revision'); END""",
    """CREATE TRIGGER ukb_refresh_immutable BEFORE UPDATE ON ukb_revision_refresh
        BEGIN SELECT RAISE(ABORT, 'Refresh claims are immutable'); END""",
    """CREATE TRIGGER ukb_refresh_retained BEFORE DELETE ON ukb_revision_refresh
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.revision_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release refresh claims'); END""",
    """CREATE TABLE ukb_refresh_proposals (
        proposal_id TEXT PRIMARY KEY REFERENCES ukb_knowledge_revisions(revision_id),
        work_key TEXT NOT NULL,
        detail_digest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        execution_id TEXT NOT NULL REFERENCES ukb_executions(execution_id),
        conflict_count INTEGER NOT NULL CHECK (conflict_count >= 0)
    )""",
    "CREATE INDEX ukb_refresh_work ON ukb_refresh_proposals(work_key)",
    """CREATE TRIGGER ukb_refresh_proposal_immutable BEFORE UPDATE ON ukb_refresh_proposals
        BEGIN SELECT RAISE(ABORT, 'Refresh proposals are immutable'); END""",
    """CREATE TRIGGER ukb_refresh_proposal_retained BEFORE DELETE ON ukb_refresh_proposals
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.proposal_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release refresh proposals'); END""",
)
