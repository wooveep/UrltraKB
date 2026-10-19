"""Explicitly fence unpublished candidates before bounded root retirement."""

from dataclasses import dataclass

from pydantic import TypeAdapter

from ultrakb.source_records import RecordId

from .coordination import storage_stage, try_reclamation
from .receipt_store import pending_reclamation_barrier


@dataclass(frozen=True)
class CandidateSummary:
    candidate_id: str
    version: str
    view_id: str
    expected_publication: str | None
    kind: str
    protected_reason: str | None


@dataclass(frozen=True)
class CandidatePage:
    kb_id: str
    generation: str
    items: tuple[CandidateSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class CandidateDiscardResult:
    status: str
    candidate_id: str
    blocked_reason: str | None = None


def _protection(connection, revision_id):
    if connection.execute(
        "SELECT 1 FROM ukb_publications WHERE revision_id = ? UNION ALL "
        "SELECT 1 FROM ukb_publication_receipts WHERE revision_id = ? UNION ALL "
        "SELECT 1 FROM ukb_sync_plans s JOIN ukb_projection_state p "
        "ON p.active_plan_id = s.plan_id WHERE s.revision_id = ? LIMIT 1",
        (revision_id,) * 3,
    ).fetchone():
        return "retained_publication"
    if connection.execute(
        "SELECT 1 FROM ukb_executions WHERE state IN ('started', 'stopping') LIMIT 1"
    ).fetchone():
        # Acceptance can use another budget; preparation may not have bound its
        # owner yet. Explicit discard is conservative while any execution runs.
        # Ordinary orphan GC remains independent of active model waits.
        return "active_execution"
    if connection.execute(
        "SELECT 1 FROM ukb_generation_plans g JOIN ukb_generation_files f ON f.plan_id = g.plan_id "
        "JOIN ukb_artifact_revisions a ON a.revision_id = f.revision_id "
        "WHERE g.knowledge_revision_id = ? OR g.plan_id = ? LIMIT 1",
        (revision_id, revision_id),
    ).fetchone():
        return "accepted_artifact"
    return None


class CandidateDiscard:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    def list(self, *, after=None, limit=63, cancelled=None):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Candidate pages require a limit of 1..128")
        if after is not None:
            TypeAdapter(RecordId).validate_python(after)
        with storage_stage(self.root, self.identity, read_only=True, cancelled=cancelled) as db:
            blocked = pending_reclamation_barrier(db)
            db.connection.execute("BEGIN")
            rows = db.connection.execute(
                "SELECT r.revision_id, r.manifest_digest, r.view_id, r.expected_publication, "
                "CASE WHEN EXISTS (SELECT 1 FROM ukb_import_candidates c "
                "WHERE c.proposal_id = r.revision_id) THEN 'import' "
                "WHEN EXISTS (SELECT 1 FROM ukb_refresh_proposals c "
                "WHERE c.proposal_id = r.revision_id) THEN 'refresh' "
                "WHEN EXISTS (SELECT 1 FROM ukb_lint_candidates c "
                "WHERE c.revision_id = r.revision_id) THEN 'lint' "
                "WHEN EXISTS (SELECT 1 FROM ukb_generation_plans c "
                "WHERE c.knowledge_revision_id = r.revision_id) THEN 'generator' "
                "ELSE 'knowledge' END "
                "FROM ukb_knowledge_revisions r WHERE r.state = 'ready' AND r.revision_id > ? "
                "AND NOT EXISTS (SELECT 1 FROM ukb_import_candidates c "
                "JOIN ukb_discarded_import_groups d ON d.original_id = c.original_id "
                "WHERE c.proposal_id = r.revision_id) "
                "ORDER BY r.revision_id LIMIT ?",
                (after or "", limit + 1),
            ).fetchall()
            rows += db.connection.execute(
                "SELECT g.plan_id, g.manifest_digest, p.view_id, g.publication_id, "
                "'generator-plan' "
                "FROM ukb_generation_plans g JOIN ukb_publications p "
                "ON p.publication_id = g.publication_id WHERE g.state = 'ready' "
                "AND g.knowledge_revision_id IS NULL AND g.plan_id > ? "
                "ORDER BY g.plan_id LIMIT ?",
                (after or "", limit + 1),
            ).fetchall()
            rows.sort(key=lambda row: row[0])
            items = tuple(
                CandidateSummary(*row, blocked or _protection(db.connection, row[0]))
                for row in rows[:limit]
            )
        return CandidatePage(
            self.identity.kb_id,
            self.identity.generation,
            items,
            items[-1].candidate_id if len(rows) > limit else None,
        )

    def discard(self, revision_id, version, *, kind="knowledge"):
        TypeAdapter(RecordId).validate_python(revision_id)
        if kind not in {"knowledge", "import", "refresh", "lint", "generator", "generator-plan"}:
            raise ValueError("Unknown candidate kind")
        if (
            not isinstance(version, str)
            or len(version) != 64
            or any(character not in "0123456789abcdef" for character in version)
        ):
            raise ValueError("Discard requires the exact candidate manifest version")
        with try_reclamation(self.root, self.identity) as db:
            if db is None:
                return CandidateDiscardResult("busy", revision_id)
            if reason := pending_reclamation_barrier(db):
                return CandidateDiscardResult("blocked", revision_id, reason)
            with db.transaction() as connection:
                table, key = (
                    ("ukb_generation_plans", "plan_id")
                    if kind == "generator-plan"
                    else ("ukb_knowledge_revisions", "revision_id")
                )
                row = connection.execute(
                    f"SELECT state, manifest_digest FROM {table} WHERE {key} = ?",
                    (revision_id,),
                ).fetchone()
                if row is None:
                    return CandidateDiscardResult("missing", revision_id)
                if row[1] != version:
                    return CandidateDiscardResult("conflict", revision_id, "candidate_changed")
                if reason := _protection(connection, revision_id):
                    return CandidateDiscardResult("blocked", revision_id, reason)
                if row[0] == "retiring":
                    return CandidateDiscardResult("discarded", revision_id)
                if row[0] != "ready":
                    return CandidateDiscardResult("blocked", revision_id, "candidate_not_ready")
                if kind == "generator-plan":
                    # Plans with knowledge changes are discarded through that
                    # knowledge candidate, preserving one coherent decision.
                    if connection.execute(
                        "SELECT knowledge_revision_id FROM ukb_generation_plans WHERE plan_id = ?",
                        (revision_id,),
                    ).fetchone() != (None,):
                        return CandidateDiscardResult("blocked", revision_id, "knowledge_candidate")
                    connection.execute(
                        "UPDATE ukb_generation_plans SET state = 'retiring' WHERE plan_id = ?",
                        (revision_id,),
                    )
                    return CandidateDiscardResult("discarded", revision_id)
                connection.execute(
                    "INSERT INTO ukb_discarded_import_groups "
                    "SELECT original_id, proposal_id, ? FROM ukb_import_candidates "
                    "WHERE proposal_id = ? ON CONFLICT(original_id) DO NOTHING",
                    (version, revision_id),
                )
                connection.execute(
                    "UPDATE ukb_knowledge_revisions SET state = 'retiring' WHERE revision_id = ?",
                    (revision_id,),
                )
                for table, key, owner in (
                    ("ukb_import_candidates", "proposal_id", "import-candidate"),
                    ("ukb_refresh_proposals", "proposal_id", "refresh-proposal"),
                    ("ukb_lint_candidates", "revision_id", "lint-candidate"),
                ):
                    connection.execute(f"DELETE FROM {table} WHERE {key} = ?", (revision_id,))
                    connection.execute(
                        "DELETE FROM ukb_blob_refs WHERE owner_kind = ? AND owner_id = ?",
                        (owner, revision_id),
                    )
                connection.execute(
                    "UPDATE ukb_generation_plans SET state = 'retiring' "
                    "WHERE knowledge_revision_id = ? AND state IN ('building', 'ready')",
                    (revision_id,),
                )
        return CandidateDiscardResult("discarded", revision_id)
