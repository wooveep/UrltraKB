"""Sealed synchronization plans, last applied files and persistent editor pauses."""

PROJECTION_SCHEMA = (
    """CREATE TABLE ukb_sync_plans (
        plan_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES ukb_knowledge_revisions(revision_id),
        view_id TEXT NOT NULL,
        base_generation INTEGER NOT NULL,
        applied_epoch INTEGER NOT NULL,
        manifest_digest TEXT REFERENCES ukb_blobs(digest),
        item_count INTEGER NOT NULL CHECK (item_count >= 0),
        remaining_count INTEGER NOT NULL CHECK (remaining_count >= 0),
        conflict_count INTEGER NOT NULL CHECK (conflict_count >= 0),
        kind TEXT NOT NULL CHECK (kind IN ('publication', 'resumption')),
        state TEXT NOT NULL CHECK (state IN
            ('draft', 'sealed', 'ready', 'active', 'completed', 'superseded',
             'retiring', 'retired')),
        CHECK ((state = 'retired') = (manifest_digest IS NULL))
    )""",
    "CREATE INDEX ukb_sync_by_view ON ukb_sync_plans(view_id, state, base_generation, plan_id)",
    """CREATE TABLE ukb_projection_state (
        view_id TEXT PRIMARY KEY,
        active_plan_id TEXT REFERENCES ukb_sync_plans(plan_id),
        target_generation INTEGER NOT NULL CHECK (target_generation >= 0),
        applied_epoch INTEGER NOT NULL CHECK (applied_epoch >= 0),
        paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
        edit_id TEXT REFERENCES ukb_wiki_edits(edit_id)
    )""",
    """CREATE TABLE ukb_projection_applied (
        view_id TEXT NOT NULL REFERENCES ukb_projection_state(view_id),
        path TEXT NOT NULL,
        digest TEXT REFERENCES ukb_blobs(digest),
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        generation INTEGER NOT NULL,
        PRIMARY KEY (view_id, path)
    )""",
    """CREATE TABLE ukb_sync_items (
        plan_id TEXT NOT NULL REFERENCES ukb_sync_plans(plan_id),
        path TEXT NOT NULL,
        expected_digest TEXT REFERENCES ukb_blobs(digest),
        expected_bytes INTEGER NOT NULL CHECK (expected_bytes >= 0),
        target_digest TEXT REFERENCES ukb_blobs(digest),
        target_bytes INTEGER NOT NULL CHECK (target_bytes >= 0),
        state TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'conflict')),
        PRIMARY KEY (plan_id, path)
    )""",
    "CREATE INDEX ukb_sync_pending ON ukb_sync_items(plan_id, state, path)",
    """CREATE INDEX ukb_sync_apply_order ON ukb_sync_items
        (plan_id, state, (target_digest IS NOT NULL), path DESC)""",
    """CREATE INDEX ukb_sync_recovery ON ukb_sync_items
        (path, target_bytes, expected_digest, target_digest, plan_id) WHERE state = 'pending'""",
    """CREATE TABLE ukb_revision_sync (
        revision_id TEXT PRIMARY KEY REFERENCES ukb_knowledge_revisions(revision_id),
        plan_id TEXT NOT NULL UNIQUE REFERENCES ukb_sync_plans(plan_id)
    )""",
    """CREATE TRIGGER ukb_revision_sync_draft BEFORE INSERT ON ukb_revision_sync
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions r JOIN ukb_sync_plans p
                         ON p.revision_id = r.revision_id AND p.plan_id = NEW.plan_id
                         WHERE r.revision_id = NEW.revision_id AND r.state = 'draft'
                         AND p.state = 'ready')
        BEGIN SELECT RAISE(ABORT, 'Revision requires its ready synchronization plan'); END""",
    """CREATE TRIGGER ukb_revision_sync_immutable BEFORE UPDATE ON ukb_revision_sync
        BEGIN SELECT RAISE(ABORT, 'Revision synchronization identity is immutable'); END""",
    """CREATE TRIGGER ukb_sync_item_insert BEFORE INSERT ON ukb_sync_items
        WHEN NOT EXISTS (SELECT 1 FROM ukb_sync_plans
                         WHERE plan_id = NEW.plan_id AND state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Only draft synchronization plans accept items'); END""",
    """CREATE TRIGGER ukb_sync_item_immutable BEFORE UPDATE OF
        plan_id, path, expected_digest, expected_bytes, target_digest, target_bytes
        ON ukb_sync_items
        BEGIN SELECT RAISE(ABORT, 'Synchronization item targets are immutable'); END""",
    """CREATE TRIGGER ukb_sync_plan_immutable BEFORE UPDATE OF
        plan_id, revision_id, view_id, base_generation, applied_epoch,
        item_count, kind
        ON ukb_sync_plans
        BEGIN SELECT RAISE(ABORT, 'Synchronization plan inputs are immutable'); END""",
    """CREATE TRIGGER ukb_sync_manifest_immutable BEFORE UPDATE OF manifest_digest
        ON ukb_sync_plans WHEN NOT (OLD.state = 'retiring' AND NEW.state = 'retired'
        AND NEW.manifest_digest IS NULL)
        BEGIN SELECT RAISE(ABORT, 'Only retired plans release their manifest'); END""",
    """CREATE TRIGGER ukb_sync_retirement_guard BEFORE UPDATE OF state ON ukb_sync_plans
        WHEN NEW.state IN ('retiring', 'retired') AND
        (EXISTS (SELECT 1 FROM ukb_projection_state WHERE active_plan_id = NEW.plan_id)
         OR (NEW.state = 'retired' AND EXISTS
             (SELECT 1 FROM ukb_sync_items WHERE plan_id = NEW.plan_id)))
        BEGIN SELECT RAISE(ABORT, 'Referenced synchronization plans cannot retire'); END""",
    """CREATE TRIGGER ukb_sync_item_delete BEFORE DELETE ON ukb_sync_items
        WHEN NOT EXISTS (SELECT 1 FROM ukb_sync_plans
                         WHERE plan_id = OLD.plan_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring synchronization plans release items'); END""",
    """CREATE TRIGGER ukb_revision_sync_delete BEFORE DELETE ON ukb_revision_sync
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.revision_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release their plan binding'); END""",
    """CREATE TABLE ukb_wiki_edits (
        edit_id TEXT PRIMARY KEY,
        view_id TEXT NOT NULL,
        baseline_manifest TEXT NOT NULL REFERENCES ukb_blobs(digest),
        baseline_generation INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('draft', 'paused', 'accepted', 'discarded')),
        publication_id TEXT REFERENCES ukb_publications(publication_id)
    )""",
    """CREATE TRIGGER ukb_edit_baseline_immutable BEFORE UPDATE OF
        edit_id, view_id, baseline_manifest, baseline_generation ON ukb_wiki_edits
        BEGIN SELECT RAISE(ABORT, 'External edit baselines are immutable'); END""",
)
