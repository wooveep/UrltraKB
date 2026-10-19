"""Publication binds one immutable import claim and its durable success receipt."""

PUBLICATION_SCHEMA = (
    """CREATE TABLE ukb_revision_source_changes (
        revision_id TEXT PRIMARY KEY REFERENCES ukb_knowledge_revisions(revision_id),
        source_id TEXT NOT NULL REFERENCES ukb_sources(source_id),
        source_revision_id TEXT NOT NULL REFERENCES ukb_source_revisions(revision_id),
        source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
        kind TEXT NOT NULL CHECK (kind IN ('withdrawn', 'empty')),
        unit_revision_id TEXT REFERENCES ukb_unit_revisions(unit_revision_id)
    )""",
    "CREATE INDEX ukb_source_change_source ON "
    "ukb_revision_source_changes(source_id, source_generation)",
    "CREATE INDEX ukb_source_change_unit ON ukb_revision_source_changes(unit_revision_id)",
    """CREATE TRIGGER ukb_source_change_draft BEFORE INSERT ON ukb_revision_source_changes
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = NEW.revision_id AND state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Source changes require a draft revision'); END""",
    """CREATE TRIGGER ukb_source_change_immutable BEFORE UPDATE ON ukb_revision_source_changes
        BEGIN SELECT RAISE(ABORT, 'Source change claims are immutable'); END""",
    """CREATE TRIGGER ukb_source_change_retained BEFORE DELETE ON ukb_revision_source_changes
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.revision_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release source change claims'); END""",
    """CREATE TABLE ukb_revision_import (
        revision_id TEXT PRIMARY KEY REFERENCES ukb_knowledge_revisions(revision_id),
        intent_id TEXT NOT NULL REFERENCES ukb_import_intents(intent_id),
        attempt_id TEXT NOT NULL REFERENCES ukb_pending_attempts(attempt_id),
        dispatch_id TEXT NOT NULL,
        generation TEXT NOT NULL,
        source_revision_id TEXT NOT NULL REFERENCES ukb_source_revisions(revision_id),
        unit_revision_id TEXT NOT NULL REFERENCES ukb_unit_revisions(unit_revision_id),
        root_import_id TEXT NOT NULL REFERENCES ukb_import_groups(root_import_id),
        outcome_json TEXT NOT NULL CHECK (
            json_valid(outcome_json) AND json_type(outcome_json) = 'object')
    )""",
    "CREATE INDEX ukb_revision_import_attempt ON ukb_revision_import(attempt_id)",
    """CREATE TRIGGER ukb_revision_import_draft BEFORE INSERT ON ukb_revision_import
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions r JOIN ukb_pending_attempts a
                         ON a.attempt_id = NEW.attempt_id AND a.intent_id = NEW.intent_id
                         AND a.dispatch_id = NEW.dispatch_id AND a.generation = NEW.generation
                         WHERE r.revision_id = NEW.revision_id AND r.state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Revision requires its exact import claim'); END""",
    """CREATE TRIGGER ukb_revision_import_immutable BEFORE UPDATE ON ukb_revision_import
        BEGIN SELECT RAISE(ABORT, 'Revision import claims are immutable'); END""",
    """CREATE TRIGGER ukb_revision_import_delete BEFORE DELETE ON ukb_revision_import
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.revision_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release import claims'); END""",
    """CREATE TABLE ukb_publication_receipts (
        publication_id TEXT PRIMARY KEY REFERENCES ukb_publications(publication_id),
        revision_id TEXT NOT NULL UNIQUE REFERENCES ukb_knowledge_revisions(revision_id),
        generation TEXT NOT NULL,
        attempt_id TEXT UNIQUE REFERENCES ukb_pending_attempts(attempt_id),
        published_at TEXT NOT NULL,
        execution_id TEXT REFERENCES ukb_executions(execution_id)
    )""",
    "CREATE INDEX ukb_publication_execution ON ukb_publication_receipts(execution_id)",
    """CREATE TRIGGER ukb_publication_receipt_owner BEFORE INSERT ON ukb_publication_receipts
        WHEN NOT EXISTS (SELECT 1 FROM ukb_publications WHERE publication_id = NEW.publication_id
                         AND revision_id = NEW.revision_id AND generation = NEW.generation)
        OR NEW.attempt_id IS NOT (SELECT attempt_id FROM ukb_revision_import
                                 WHERE revision_id = NEW.revision_id)
        BEGIN SELECT RAISE(ABORT, 'Receipt requires its exact publication'); END""",
    """CREATE TRIGGER ukb_publication_receipt_immutable BEFORE UPDATE ON ukb_publication_receipts
        BEGIN SELECT RAISE(ABORT, 'Publication receipts are immutable'); END""",
    """CREATE TRIGGER ukb_publication_receipt_retained BEFORE DELETE ON ukb_publication_receipts
        BEGIN SELECT RAISE(ABORT, 'Publication receipts are retained knowledge history'); END""",
    """CREATE TRIGGER ukb_import_success_receipt BEFORE UPDATE ON ukb_pending_attempts
        WHEN NEW.status = 'completed'
        AND EXISTS (SELECT 1 FROM ukb_import_intents WHERE intent_id = NEW.intent_id
                    AND kind = 'import' AND unit_revision_id IS NOT NULL)
        AND NOT EXISTS (SELECT 1 FROM ukb_publication_receipts r
                        WHERE r.attempt_id = NEW.attempt_id
                        AND r.publication_id = NEW.publication_id AND r.generation = NEW.generation)
        BEGIN SELECT RAISE(ABORT, 'Import success requires its committed receipt'); END""",
    """CREATE TRIGGER ukb_import_success_insert BEFORE INSERT ON ukb_pending_attempts
        WHEN NEW.status = 'completed'
        AND EXISTS (SELECT 1 FROM ukb_import_intents WHERE intent_id = NEW.intent_id
                    AND kind = 'import' AND unit_revision_id IS NOT NULL)
        AND NOT EXISTS (SELECT 1 FROM ukb_publication_receipts r
                        WHERE r.attempt_id = NEW.attempt_id
                        AND r.publication_id = NEW.publication_id AND r.generation = NEW.generation)
        BEGIN SELECT RAISE(ABORT, 'Import success requires its committed receipt'); END""",
    """CREATE TRIGGER ukb_completed_attempt_immutable BEFORE UPDATE ON ukb_pending_attempts
        WHEN OLD.status = 'completed'
        BEGIN SELECT RAISE(ABORT, 'Completed attempts are immutable history'); END""",
    """CREATE TRIGGER ukb_pending_attempt_identity BEFORE UPDATE OF
        attempt_id, intent_id, dispatch_id, generation ON ukb_pending_attempts
        BEGIN SELECT RAISE(ABORT, 'Attempt identities are immutable'); END""",
)
