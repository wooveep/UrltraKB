"""Publish one document unit with immutable evidence and a business checkpoint."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from openkb.file_state import contained_paths
from openkb.ingest_diagnostics import failure_reason
from openkb.ingest_records import (
    ImportUnit,
    KnowledgeHead,
    KnowledgeRevision,
    Proposal,
    SourceMap,
    UnitPublication,
    UnitRevision,
)
from openkb.knowledge_scope import KnowledgeScope, live_scope
from openkb.lifecycle import current_generation
from openkb.locks import LockCancelled, kb_ingest_lock, kb_read_lock
from openkb.mutation import RecoveryRequired, _copy_file_atomic, mutation_scope
from openkb.processing_policy import ProcessingDecision
from openkb.source_catalog import (
    Admission,
    read_record,
    read_source,
    read_source_revision,
    record_path,
    write_record,
)
from openkb.source_records import DiscoveryIntent, Source
from openkb.state import HashRegistry


def list_source_units(kb_dir: Path, source_id: str) -> tuple[ImportUnit, ...]:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        units = (
            read_record(root, "units", path.stem, ImportUnit)
            for path in sorted((root / ".openkb/catalog/units").glob("*.json"))
        )
        return tuple(unit for unit in units if unit.source_id == source_id)


def publication_id(unit_id: str, view_id: str) -> str:
    return hashlib.sha256(f"{unit_id}:{view_id}".encode()).hexdigest()[:32]


def read_unit_publication(kb_dir: Path, unit_id: str, view_id: str = "legacy") -> UnitPublication:
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        state = read_record(root, "publications", publication_id(unit_id, view_id), UnitPublication)
        if state.unit_id != unit_id or state.view_id != view_id:
            raise ValueError("Publication belongs to another unit or view")
        return state


def plan_import_units(
    kb_dir: Path,
    admission: Admission,
    fingerprint: str,
    *,
    key: str = "body",
    doc_name: str | None = None,
    name: str | None = None,
) -> tuple[ImportUnit, UnitRevision]:
    """A native document has one stable body; a processing change creates a target."""
    root = kb_dir.resolve()
    with kb_ingest_lock(root / ".openkb"):
        units = list_source_units(root, admission.source.source_id)
        known = next((unit for unit in units if unit.key == key), None)
        if known:
            previous = read_record(root, "unit-revisions", known.target_revision_id, UnitRevision)
            if (
                previous.source_revision_id == admission.revision.source_revision_id
                and previous.processing_fingerprint == fingerprint
                and previous.annotation_id == admission.source.annotation_id
            ):
                return known, previous
        unit_id = known.unit_id if known else uuid.uuid4().hex
        if not known and key != "body" and doc_name:
            from openkb.source_catalog import occupied_document_names

            if doc_name in occupied_document_names(root):
                doc_name += f"-{unit_id}"
        revision = UnitRevision(
            unit_revision_id=uuid.uuid4().hex,
            unit_id=unit_id,
            source_revision_id=admission.revision.source_revision_id,
            processing_fingerprint=fingerprint,
            job_id=uuid.uuid4().hex,
            annotation_id=admission.source.annotation_id,
        )
        unit = ImportUnit(
            unit_id=unit_id,
            source_id=admission.source.source_id,
            key=key,
            doc_name=known.doc_name if known else doc_name or admission.source.doc_name,
            target_revision_id=revision.unit_revision_id,
            generation=(known.generation + 1 if known else 1),
            name=name if name is not None else known.name if known else None,
        )
        records = {
            record_path(root, "units", unit_id): unit,
            record_path(root, "unit-revisions", revision.unit_revision_id): revision,
        }
        with mutation_scope(root, list(records), operation="plan-import-unit"):
            for path, record in records.items():
                write_record(path, record)
        return unit, revision


def begin_unit_attempt(
    kb_dir: Path,
    unit: ImportUnit,
    revision: UnitRevision,
    *,
    discovery_intent: DiscoveryIntent | None = None,
    recompile: bool = False,
    view_id: str = "legacy",
    retry_confirmed: bool = False,
) -> tuple[UnitPublication, bool]:
    identity = publication_id(unit.unit_id, view_id)
    path = record_path(kb_dir, "publications", identity)
    previous = (
        read_record(kb_dir, "publications", identity, UnitPublication) if path.exists() else None
    )
    if previous and previous.status == "started":
        interrupted = previous.model_copy(
            update={
                "status": "interrupted",
                "message": "Previous result is unconfirmed; review it before an explicit retry",
            }
        )
        old_attempt = record_path(kb_dir, "attempts", interrupted.attempt_id)
        with mutation_scope(kb_dir, [path, old_attempt], operation="record-interrupted-attempt"):
            write_record(path, interrupted)
            write_record(old_attempt, interrupted)
        previous = interrupted
    if (
        previous
        and previous.target_revision_id == revision.unit_revision_id
        and (
            previous.status == "awaiting_confirmation"
            or previous.status in {"interrupted", "blocked"}
            and not retry_confirmed
            or previous.status in {"completed", "empty", "retired"}
            and not recompile
        )
    ):
        return previous, False
    state = UnitPublication(
        publication_id=identity,
        unit_id=unit.unit_id,
        view_id=view_id,
        target_revision_id=revision.unit_revision_id,
        attempt_id=uuid.uuid4().hex,
        job_id=revision.job_id,
        status="started",
        successful_revision_id=previous.successful_revision_id if previous else None,
        knowledge_revision_id=previous.knowledge_revision_id if previous else None,
    )
    attempt = record_path(kb_dir, "attempts", state.attempt_id)
    intent_path = (
        record_path(kb_dir, "discovery-intents", discovery_intent.intent_id)
        if discovery_intent and discovery_intent.cancelled
        else None
    )
    with mutation_scope(
        kb_dir,
        [
            path,
            attempt,
            *([intent_path] if intent_path else []),
        ],
        operation="begin-unit-attempt",
    ):
        write_record(path, state)
        write_record(attempt, state)
        if intent_path and discovery_intent:
            write_record(intent_path, discovery_intent.model_copy(update={"cancelled": False}))
    return state, True


def read_head(kb_dir: Path, view_id: str = "legacy") -> KnowledgeHead:
    live_scope(kb_dir, view_id)
    path = kb_dir / ".openkb/knowledge" / view_id / "head.json"
    contained_paths(kb_dir, [path])
    head = (
        KnowledgeHead.model_validate_json(path.read_text("utf-8"))
        if path.exists()
        else KnowledgeHead(view_id=view_id)
    )

    if head.view_id != view_id:
        raise ValueError("Knowledge head belongs to another view")
    return head


def wiki_versions(kb_dir: Path, wiki: Path) -> dict[str, str]:
    contained_paths(kb_dir, [wiki])
    paths = sorted(wiki.rglob("*")) if wiki.exists() else []
    if any(path.is_symlink() for path in paths):
        raise ValueError("Knowledge revisions cannot contain symbolic links")
    return {
        path.relative_to(wiki).as_posix(): HashRegistry.hash_file(path)
        for path in paths
        if path.is_file()
    }


def copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Knowledge revisions cannot contain symbolic links")
        if path.is_file():
            _copy_file_atomic(path, target / path.relative_to(source))


@dataclass(frozen=True)
class CompileView:
    scope: KnowledgeScope
    head: KnowledgeHead
    base_pages: dict[str, str]
    kb_generation: str


@contextmanager
def prepare_compile_view(kb_dir: Path, view_id: str = "legacy") -> Iterator[CompileView]:
    """Use the real KB environment with a private copy of its knowledge content."""
    live = live_scope(kb_dir, view_id)
    stage = kb_dir / ".openkb/staging" / f"unit-{uuid.uuid4().hex}"
    view = CompileView(
        KnowledgeScope(kb_dir, stage / "wiki", view_id),
        read_head(kb_dir, view_id),
        wiki_versions(kb_dir, live.wiki_dir),
        current_generation(kb_dir),
    )
    preserve_evidence = False
    try:
        with mutation_scope(kb_dir, [stage], operation="prepare-compile-view"):
            copy_tree(live.wiki_dir, view.scope.wiki_dir)
            from openkb.source_changes import filter_compile_context

            filter_compile_context(kb_dir, view.scope.wiki_dir, view.head)
        yield view
    except RecoveryRequired:
        preserve_evidence = True
        raise
    finally:
        if not preserve_evidence:
            shutil.rmtree(stage, ignore_errors=True)


def record_unit_failure(
    kb_dir: Path,
    state: UnitPublication,
    stage: str,
    error: Exception,
    *,
    discovery_intent: DiscoveryIntent | None = None,
) -> UnitPublication:
    """Called after rollback; preserve the exact previous successful revision."""
    failed = state.model_copy(
        update={
            "status": "stopped" if isinstance(error, LockCancelled) else "failed",
            "stage": stage,
            "error_type": type(error).__name__,
            "message": f"{stage}: {failure_reason(error)} ({type(error).__name__})",
        }
    )
    current = read_unit_publication(kb_dir, state.unit_id, state.view_id)
    attempt = record_path(kb_dir, "attempts", state.attempt_id)
    paths = [attempt]
    if (
        current.target_revision_id == state.target_revision_id
        and current.attempt_id == state.attempt_id
    ):
        paths.append(record_path(kb_dir, "publications", state.publication_id))
    intent_path = (
        record_path(kb_dir, "discovery-intents", discovery_intent.intent_id)
        if discovery_intent and isinstance(error, LockCancelled)
        else None
    )
    with mutation_scope(
        kb_dir, [*paths, *([intent_path] if intent_path else [])], operation="record-unit-failure"
    ):
        for path in paths:
            write_record(path, failed)
        if intent_path and discovery_intent:
            write_record(intent_path, discovery_intent.model_copy(update={"cancelled": True}))
    return failed


def _check_target(
    kb_dir: Path, source: Source, unit: ImportUnit, state: UnitPublication, view: CompileView
) -> None:
    if current_generation(kb_dir) != view.kb_generation:
        raise ValueError("Knowledge base generation changed")
    current_source = read_source(kb_dir, source.source_id)
    current_unit = read_record(kb_dir, "units", unit.unit_id, ImportUnit)
    current_state = read_unit_publication(kb_dir, unit.unit_id, state.view_id)
    if (
        current_source.removed
        or current_source.target_generation != source.target_generation
        or current_source.target_revision_id != source.target_revision_id
        or current_unit != unit
        or current_state.attempt_id != state.attempt_id
        or state.view_id != view.scope.view_id
    ):
        raise ValueError("Import target was replaced by a newer request")


def publish_unit_revision(
    kb_dir: Path,
    admission: Admission,
    unit: ImportUnit,
    revision: UnitRevision,
    state: UnitPublication,
    view: CompileView,
    *,
    normalized_source: str,
    source_map: SourceMap | None = None,
    processing: ProcessingDecision | None = None,
    is_long: bool,
    normalized_format: Literal["pdf", "markdown"] = "pdf",
    index_ref: str | None = None,
    check_stop: Callable[[], None] = lambda: None,
) -> UnitPublication:
    """The knowledge head, exact input, output and checkpoint commit together."""
    check_stop()
    _check_target(kb_dir, admission.source, unit, state, view)
    live = live_scope(kb_dir, state.view_id).wiki_dir
    current_head = read_head(kb_dir, state.view_id)
    current_pages = wiki_versions(kb_dir, live)
    from openkb.source_changes import retain_pending_pages

    with mutation_scope(kb_dir, [view.scope.wiki_dir], operation="retain-pending-knowledge"):
        retain_pending_pages(kb_dir, view.scope.wiki_dir, view.head)
    generated = wiki_versions(kb_dir, view.scope.wiki_dir)
    changed = {
        name
        for name in view.base_pages.keys() | generated.keys()
        if view.base_pages.get(name) != generated.get(name)
    }
    protected = {
        name
        for name in changed
        if name.split("/")[0] in {"summaries", "concepts", "entities"}
        and (name in view.base_pages or name in view.head.generated_baselines)
        and view.head.generated_baselines.get(name) != view.base_pages.get(name)
    }
    if current_head != view.head or current_pages != view.base_pages:
        protected.add("knowledge_baseline_changed")
    inputs = {**view.head.inputs, unit.unit_id: revision.unit_revision_id}
    from openkb.knowledge_evidence import publication_dependencies

    dependencies = publication_dependencies(
        kb_dir, view.head, generated, view.base_pages, inputs.values()
    )
    baselines = dict(view.head.generated_baselines)
    for name in changed:
        if name in generated:
            baselines[name] = generated[name]
        else:
            baselines.pop(name, None)
    originals = set()
    referenced_inputs = set(inputs.values()) | {
        identity for values in dependencies.values() for identity in values
    }
    for input_id in referenced_inputs:
        input_revision = read_record(kb_dir, "unit-revisions", input_id, UnitRevision)
        source_revision = read_source_revision(kb_dir, input_revision.source_revision_id)
        originals.add(source_revision.original)
        originals.update(asset.artifact for asset in source_revision.assets if asset.artifact)
    knowledge_id = uuid.uuid4().hex
    manifest = KnowledgeRevision(
        knowledge_revision_id=knowledge_id,
        view_id=state.view_id,
        base_revision_id=view.head.knowledge_revision_id,
        unit_revision_id=revision.unit_revision_id,
        input_revisions=tuple(sorted(inputs.values())),
        page_dependencies=dependencies,
        generated_baselines=baselines,
        original_references=tuple(sorted(originals)),
        normalized_source=normalized_source,
        source_map=source_map,
        source_format=admission.revision.source_format,
        normalized_format=normalized_format,
        length_class=processing.length_class if processing else ("long" if is_long else "short"),
        execution_mode="segmented" if is_long else "full",
        processing=processing,
        index_ref=index_ref,
    )
    state_path = record_path(kb_dir, "publications", state.publication_id)
    attempt_path = record_path(kb_dir, "attempts", state.attempt_id)
    if protected:
        proposal_id = uuid.uuid4().hex
        directory = kb_dir / ".openkb/proposals" / proposal_id
        proposal = Proposal(
            proposal_id=proposal_id,
            unit_revision_id=revision.unit_revision_id,
            view_id=state.view_id,
            expected_source_generation=admission.source.target_generation,
            expected_unit_generation=unit.generation,
            expected_view_generation=current_head.generation,
            expected_kb_generation=view.kb_generation,
            expected_pages=current_pages,
            candidate_pages=generated,
            conflicts=tuple(sorted(protected)),
            candidate_manifest=manifest,
        )
        awaiting = state.model_copy(
            update={
                "status": "awaiting_confirmation",
                "proposal_id": proposal_id,
                "stage": "publication",
                "message": "Knowledge changes require explicit review",
            }
        )
        with mutation_scope(
            kb_dir, [directory, state_path, attempt_path], operation="save-unit-proposal"
        ):
            copy_tree(view.scope.wiki_dir, directory / "wiki")
            copy_tree(live, directory / "base")
            if index_ref:
                copy_tree(view.scope.wiki_dir.parent / "index", directory / "index")
            write_record(directory / "proposal.json", proposal)
            write_record(state_path, awaiting)
            write_record(attempt_path, awaiting)
            check_stop()
            _check_target(kb_dir, admission.source, unit, awaiting, view)
        return awaiting
    return commit_unit_revision(
        kb_dir, admission, unit, revision, state, view, manifest, check_stop=check_stop
    )


def commit_unit_revision(
    kb_dir: Path,
    admission: Admission,
    unit: ImportUnit,
    revision: UnitRevision,
    state: UnitPublication,
    view: CompileView,
    manifest: KnowledgeRevision,
    *,
    check_stop: Callable[[], None] = lambda: None,
) -> UnitPublication:
    """Commit a generated or explicitly reviewed candidate and its exact checkpoint."""
    check_stop()
    _check_target(kb_dir, admission.source, unit, state, view)
    if read_head(kb_dir, state.view_id) != view.head:
        raise ValueError("Knowledge view changed before publication")
    live = live_scope(kb_dir, state.view_id).wiki_dir
    if wiki_versions(kb_dir, live) != view.base_pages:
        raise ValueError("Knowledge pages changed before publication")
    generated = wiki_versions(kb_dir, view.scope.wiki_dir)
    knowledge_id = manifest.knowledge_revision_id
    directory = kb_dir / ".openkb/knowledge" / state.view_id / "revisions" / knowledge_id
    if directory.exists():
        raise ValueError("An immutable knowledge revision already exists")
    state_path = record_path(kb_dir, "publications", state.publication_id)
    attempt_path = record_path(kb_dir, "attempts", state.attempt_id)
    head_path = kb_dir / ".openkb/knowledge" / state.view_id / "head.json"
    head = KnowledgeHead(
        view_id=state.view_id,
        generation=view.head.generation + 1,
        knowledge_revision_id=knowledge_id,
        inputs={**view.head.inputs, unit.unit_id: revision.unit_revision_id},
        generated_baselines=manifest.generated_baselines,
        needs_refresh=view.head.needs_refresh,
    )
    completed = state.model_copy(
        update={
            "status": "completed",
            "successful_revision_id": revision.unit_revision_id,
            "knowledge_revision_id": knowledge_id,
            "stage": "published",
            "error_type": None,
            "message": None,
        }
    )
    with mutation_scope(
        kb_dir,
        [directory, live, head_path, state_path, attempt_path],
        operation="publish-unit-revision",
    ):
        copy_tree(view.scope.wiki_dir, directory / "wiki")
        if manifest.index_ref:
            copy_tree(view.scope.wiki_dir.parent / "index", directory / "index")
        write_record(directory / "manifest.json", manifest)
        write_record(directory / "publication.json", completed)
        copy_tree(view.scope.wiki_dir, live)
        for name in view.base_pages.keys() - generated.keys():
            (live / name).unlink(missing_ok=True)
        write_record(head_path, head)
        write_record(state_path, completed)
        write_record(attempt_path, completed)
        check_stop()
        _check_target(kb_dir, admission.source, unit, completed, view)
    return completed
