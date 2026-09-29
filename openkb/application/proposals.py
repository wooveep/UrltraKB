"""Review and explicitly accept a durable candidate without repeating model work."""

from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from openkb.application.execution import ExecutionContext
from openkb.application.ingestion import result_from_publication
from openkb.file_state import contained_paths
from openkb.ingest_records import ImportUnit, Proposal, UnitPublication, UnitRevision
from openkb.ingest_result import IngestResult
from openkb.knowledge_scope import KnowledgeScope, live_scope, resolve_scope
from openkb.lifecycle import current_generation
from openkb.locks import LockCancelled, kb_ingest_lock, kb_read_lock
from openkb.mutation import RecoveryRequired, mutation_scope
from openkb.source_catalog import (
    Admission,
    read_record,
    read_source,
    read_source_revision,
    record_path,
    write_record,
)
from openkb.source_records import DiscoveryIntent, RecordId
from openkb.state import HashRegistry
from openkb.unit_publication import (
    CompileView,
    commit_unit_revision,
    read_head,
    read_unit_publication,
    record_unit_failure,
    wiki_versions,
)


@dataclass(frozen=True)
class ProposalView:
    proposal_id: str
    source_id: str
    unit_id: str
    view_id: str
    status: str
    version: str
    conflicts: tuple[str, ...]
    diff: str


def _read(kb_dir: Path, identity: str) -> tuple[Path, Proposal]:
    TypeAdapter(RecordId).validate_python(identity)
    directory = kb_dir / ".openkb/proposals" / identity
    contained_paths(kb_dir, [directory])
    proposal = Proposal.model_validate_json((directory / "proposal.json").read_text("utf-8"))
    if proposal.proposal_id != identity:
        raise ValueError("Proposal identity does not match its location")
    if (
        proposal.candidate_manifest.unit_revision_id != proposal.unit_revision_id
        or proposal.candidate_manifest.view_id != proposal.view_id
        or proposal.candidate_manifest.normalized_source not in proposal.candidate_pages
    ):
        raise ValueError("Candidate belongs to another proposal input or view")
    if wiki_versions(kb_dir, directory / "wiki") != proposal.candidate_pages:
        raise ValueError("Saved proposal content changed")
    if wiki_versions(kb_dir, directory / "base") != proposal.expected_pages:
        raise ValueError("Saved proposal comparison base changed")
    return directory, proposal


def _diff(directory: Path, proposal: Proposal) -> str:
    chunks = []
    for name in sorted(proposal.expected_pages.keys() | proposal.candidate_pages.keys()):
        if proposal.expected_pages.get(name) == proposal.candidate_pages.get(name):
            continue
        if not name.endswith((".md", ".json", ".txt")):
            chunks.append(f"Asset changed: {name}\n")
            continue
        old, new = directory / "base" / name, directory / "wiki" / name
        before = old.read_text("utf-8") if old.is_file() else ""
        after = new.read_text("utf-8") if new.is_file() else ""
        chunks.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"current/{name}",
                tofile=f"candidate/{name}",
            )
        )
    return "".join(chunks)


def read_proposal(
    kb_dir: Path, proposal_id: str, *, scope: KnowledgeScope | None = None
) -> ProposalView:
    root = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(root, scope)
    with kb_read_lock(root / ".openkb"):
        directory, proposal = _read(root, proposal_id)
        if scope and (scope.view_id != proposal.view_id or scope.read_only):
            raise ValueError("Proposal does not belong to the selected live view")
        revision = read_record(root, "unit-revisions", proposal.unit_revision_id, UnitRevision)
        unit = read_record(root, "units", revision.unit_id, ImportUnit)
        state = read_unit_publication(root, unit.unit_id, proposal.view_id)
        return ProposalView(
            proposal_id,
            unit.source_id,
            unit.unit_id,
            proposal.view_id,
            state.status if state.proposal_id == proposal_id else "superseded",
            HashRegistry.hash_file(directory / "proposal.json"),
            proposal.conflicts,
            _diff(directory, proposal),
        )


def list_proposals(
    kb_dir: Path, *, scope: KnowledgeScope | None = None
) -> tuple[ProposalView, ...]:
    root = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(root, scope)
        if scope.read_only:
            return ()
    with kb_read_lock(root / ".openkb"):
        views = (
            read_proposal(root, path.parent.name)
            for path in sorted((root / ".openkb/proposals").glob("*/proposal.json"))
        )
        return tuple(
            view
            for view in views
            if view.status not in {"completed", "superseded"}
            and (scope is None or view.view_id == scope.view_id)
        )


def accept_proposal(
    kb_dir: Path,
    proposal_id: str,
    *,
    version: str,
    context: ExecutionContext | None = None,
    scope: KnowledgeScope | None = None,
) -> IngestResult:
    root = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(root, scope, writable=True)
    check_stop = context.check_stop if context else lambda: None
    with kb_ingest_lock(
        root / ".openkb",
        cancelled=context.cancelled if context else None,
        on_wait=context.waiting if context else None,
    ):
        check_stop()
        directory, proposal = _read(root, proposal_id)
        if scope and scope.view_id != proposal.view_id:
            raise ValueError("Proposal belongs to another knowledge view")
        revision = read_record(root, "unit-revisions", proposal.unit_revision_id, UnitRevision)
        unit = read_record(root, "units", revision.unit_id, ImportUnit)
        source = read_source(root, unit.source_id)
        frozen = read_source_revision(root, revision.source_revision_id)
        if frozen.source_id != source.source_id:
            raise ValueError("Proposal input belongs to another source")
        intent = read_record(root, "discovery-intents", frozen.discovery_intent_id, DiscoveryIntent)
        admission = Admission(source, frozen, intent)
        state = read_unit_publication(root, unit.unit_id, proposal.view_id)
        receipt = (
            root
            / ".openkb/knowledge"
            / proposal.view_id
            / "revisions"
            / proposal.candidate_manifest.knowledge_revision_id
            / "publication.json"
        )
        contained_paths(root, [receipt])
        if receipt.exists():
            accepted = UnitPublication.model_validate_json(receipt.read_text("utf-8"))
            if (
                accepted.proposal_id != proposal_id
                or accepted.unit_id != unit.unit_id
                or accepted.successful_revision_id != revision.unit_revision_id
                or accepted.status != "completed"
                or accepted.view_id != proposal.view_id
                or accepted.knowledge_revision_id
                != proposal.candidate_manifest.knowledge_revision_id
            ):
                raise ValueError("Proposal completion checkpoint is inconsistent")
            return result_from_publication(root, admission, accepted, status="skipped")
        head = read_head(root, proposal.view_id)
        pages = wiki_versions(root, live_scope(root, proposal.view_id).wiki_dir)
        if (
            version != HashRegistry.hash_file(directory / "proposal.json")
            or source.removed
            or source.target_generation != proposal.expected_source_generation
            or unit.generation != proposal.expected_unit_generation
            or state.target_revision_id != revision.unit_revision_id
            or state.proposal_id != proposal_id
            or head.generation != proposal.expected_view_generation
            or head.knowledge_revision_id != proposal.candidate_manifest.base_revision_id
            or pages != proposal.expected_pages
            or current_generation(root) != proposal.expected_kb_generation
        ):
            conflict = state.model_copy(
                update={
                    "message": "Pages or inputs changed; the proposal is preserved for review",
                }
            )
            return result_from_publication(root, admission, conflict, status="blocked")
        state = state.model_copy(
            update={
                "attempt_id": uuid.uuid4().hex,
                "status": "started",
                "stage": "publication",
                "error_type": None,
                "message": None,
            }
        )
        publication = record_path(root, "publications", state.publication_id)
        attempt = record_path(root, "attempts", state.attempt_id)
        intent_path = record_path(root, "discovery-intents", intent.intent_id)
        intent = intent.model_copy(update={"cancelled": False})
        admission = Admission(source, frozen, intent)
        with mutation_scope(
            root, [publication, attempt, intent_path], operation="begin-proposal-acceptance"
        ):
            write_record(publication, state)
            write_record(attempt, state)
            write_record(intent_path, intent)
        view = CompileView(
            KnowledgeScope(root, directory / "wiki", proposal.view_id),
            head,
            pages,
            proposal.expected_kb_generation,
        )
        try:
            completed = commit_unit_revision(
                root,
                admission,
                unit,
                revision,
                state,
                view,
                proposal.candidate_manifest,
                check_stop=check_stop,
            )
        except RecoveryRequired:
            raise
        except Exception as exc:
            failed = record_unit_failure(root, state, "publication", exc, discovery_intent=intent)
            if isinstance(exc, LockCancelled):
                raise
            return result_from_publication(root, admission, failed, status="failed")
        return result_from_publication(root, admission, completed, status="added")
