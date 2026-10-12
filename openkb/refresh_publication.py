"""Publish a whole refreshed view against an exact input and knowledge generation."""

import difflib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import Field, TypeAdapter

from openkb.catalog_schema import CatalogSchema, catalog_schema_path
from openkb.file_state import contained_paths
from openkb.ingest_records import KnowledgeHead, KnowledgeRevision, RefreshReason
from openkb.knowledge_scope import live_scope
from openkb.lifecycle import current_generation
from openkb.locks import durable_unlink
from openkb.mutation import mutation_scope
from openkb.source_catalog import read_source, write_record
from openkb.source_records import Digest, Record, RecordId, RelativePath, ViewId
from openkb.state import HashRegistry
from openkb.unit_publication import CompileView, copy_tree, read_head, wiki_versions


@dataclass(frozen=True)
class RefreshResult:
    status: str
    view_id: str
    knowledge_revision_id: str | None = None
    proposal_id: str | None = None
    message: str = ""
    resources: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()


class RefreshedPage(Record):
    """Bind a shared-page update to its old draft and fully read frozen inputs."""

    input_revisions: tuple[RecordId, ...]
    base_digest: Digest
    candidate_digest: Digest
    original_files: dict[RelativePath, Digest]


class RefreshSelection(Record):
    """Exact pending reasons resolved by a bounded, source-selected operation."""

    source_ids: tuple[RecordId, ...]
    page_paths: tuple[RelativePath, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    resolved: dict[RelativePath, tuple[RefreshReason, ...]] = Field(default_factory=dict)
    summary_owners: dict[RelativePath, RecordId] = Field(default_factory=dict)
    reconciled_pages: dict[RelativePath, RefreshedPage] = Field(default_factory=dict)


class RefreshProposal(Record):
    proposal_id: RecordId
    view_id: ViewId
    expected_head: KnowledgeHead
    expected_kb_generation: str
    source_generations: dict[RecordId, int]
    expected_pages: dict[RelativePath, Digest]
    candidate_pages: dict[RelativePath, Digest]
    candidate_manifest: KnowledgeRevision
    inputs: dict[RecordId, RecordId]
    conflicts: tuple[str, ...]
    selection: RefreshSelection | None = None


def remaining_refresh(head: KnowledgeHead, selection: RefreshSelection | None):
    if selection is None:
        return {}
    if not selection.source_ids or len(set(selection.source_ids)) != len(selection.source_ids):
        raise ValueError("Selected refresh requires distinct sources")
    from openkb.selected_refresh_evidence import selected_pages

    pages = selected_pages(
        {
            page: reasons
            for page, reasons in head.needs_refresh.items()
            if any(reason.source_id in selection.source_ids for reason in reasons)
        },
        selection.page_paths,
    )
    if (
        set(selection.resolved) - pages - {"index.md"}
        or set(selection.summary_owners) - pages
        or set(selection.reconciled_pages) - pages
    ):
        raise ValueError("Refresh outcome is outside selected page scope")
    for page, resolved in selection.resolved.items():
        if any(
            reason not in head.needs_refresh.get(page, ())
            or reason.source_id not in selection.source_ids
            for reason in resolved
        ):
            raise ValueError("Refresh resolution does not match the captured reason")
        if page not in {*selection.summary_owners, *selection.reconciled_pages, "index.md"}:
            raise ValueError("Pending page has no explicit refresh outcome")
    return {
        page: remaining
        for page, reasons in head.needs_refresh.items()
        if (remaining := tuple(r for r in reasons if r not in selection.resolved.get(page, ())))
    }


def selected_dependencies(kb_dir, head, pages, selection):
    """Retain untouched bases; replace only inputs explicitly reconciled on that page."""
    from openkb.knowledge_evidence import validated_input

    root = kb_dir / ".openkb/knowledge" / head.view_id / "revisions"
    previous = KnowledgeRevision.model_validate_json(
        (root / str(head.knowledge_revision_id) / "manifest.json").read_text()
    )
    result = {}
    for page in pages:
        if not page.endswith(".md"):
            continue
        if owner := selection.summary_owners.get(page):
            if owner not in head.inputs.values():
                raise ValueError("Summary owner is outside current inputs")
            result[page] = (owner,)
            continue
        dependencies = set(previous.page_dependencies.get(page, ()))
        resolved_sources = {r.source_id for r in selection.resolved.get(page, ())}
        if page in selection.reconciled_pages:
            dependencies.update(selection.reconciled_pages[page].input_revisions)
        for identity in tuple(dependencies):
            unit, source = validated_input(kb_dir, identity, head.view_id)
            if source.source_id in resolved_sources:
                dependencies.remove(identity)
                dependencies.add(head.inputs[unit.unit_id])
        result[page] = tuple(sorted(dependencies))
    return result


def candidate_current(kb_dir: Path, proposal: RefreshProposal) -> bool:
    return (
        read_head(kb_dir, proposal.view_id) == proposal.expected_head
        and current_generation(kb_dir) == proposal.expected_kb_generation
        and wiki_versions(kb_dir, live_scope(kb_dir, proposal.view_id).wiki_dir)
        == proposal.expected_pages
        and all(
            read_source(kb_dir, source).target_generation == generation
            for source, generation in proposal.source_generations.items()
        )
    )


def commit_refresh(
    kb_dir: Path,
    directory: Path,
    proposal: RefreshProposal,
    check_stop: Callable[[], None] = lambda: None,
) -> RefreshResult:
    if not candidate_current(kb_dir, proposal):
        return RefreshResult(
            "conflict",
            proposal.view_id,
            proposal_id=proposal.proposal_id,
            message="Sources or knowledge changed; refresh again.",
        )
    if proposal.selection is not None:
        from openkb.selected_refresh_evidence import validate_selection

        validate_selection(kb_dir, proposal, directory / "wiki")
    check_stop()
    manifest = proposal.candidate_manifest
    root = kb_dir / ".openkb/knowledge" / proposal.view_id
    snapshot = root / "revisions" / manifest.knowledge_revision_id
    live = live_scope(kb_dir, proposal.view_id).wiki_dir
    if snapshot.exists():
        raise ValueError("Knowledge revision is immutable")
    head = KnowledgeHead(
        view_id=proposal.view_id,
        generation=proposal.expected_head.generation + 1,
        knowledge_revision_id=manifest.knowledge_revision_id,
        inputs=proposal.inputs,
        generated_baselines=manifest.generated_baselines,
        needs_refresh=remaining_refresh(proposal.expected_head, proposal.selection),
    )
    schema = catalog_schema_path(kb_dir)
    with mutation_scope(
        kb_dir, [snapshot, live, root / "head.json", schema], operation="publish-refresh"
    ):
        copy_tree(directory / "wiki", snapshot / "wiki")
        if (directory / "index").exists():
            copy_tree(directory / "index", snapshot / "index")
        write_record(snapshot / "manifest.json", manifest)
        if proposal.selection is not None:
            write_record(snapshot / "refresh-selection.json", proposal.selection)
        copy_tree(directory / "wiki", live)
        for name in proposal.expected_pages.keys() - proposal.candidate_pages.keys():
            durable_unlink(live / name, missing_ok=True)
        # The lease prevents interleaving, and the final generation fence rejects stopped work.
        check_stop()
        if current_generation(kb_dir) != proposal.expected_kb_generation:
            raise ValueError("Knowledge base generation changed")
        write_record(root / "head.json", head)
        write_record(schema, CatalogSchema())
    return RefreshResult(
        "partial" if head.needs_refresh else "completed",
        proposal.view_id,
        manifest.knowledge_revision_id,
        resources=(snapshot.relative_to(kb_dir).as_posix(),),
        unfinished=("refresh",) if head.needs_refresh else (),
    )


def refresh_baselines(head, before, after, *, retain_unchanged=False):
    """Preserve manual edit detection for pages outside a selected refresh."""
    generated = {
        path: digest
        for path, digest in after.items()
        if not retain_unchanged or before.get(path) != digest
    }
    baselines = {**head.generated_baselines, **generated}
    for name in before.keys() - after.keys():
        baselines.pop(name, None)
    return baselines


def publish_refresh(
    kb_dir: Path,
    view: CompileView,
    inputs: dict[str, str],
    originals: tuple[str, ...],
    generations: dict[str, int],
    check_stop: Callable[[], None],
    *,
    selection: RefreshSelection | None = None,
) -> RefreshResult:
    generated = wiki_versions(kb_dir, view.scope.wiki_dir)
    changed = {
        p
        for p in generated.keys() | view.base_pages.keys()
        if generated.get(p) != view.base_pages.get(p)
    }
    protected = {
        p
        for p in changed
        if p.startswith(("summaries/", "concepts/", "entities/", "reports/", "explorations/"))
        and (p in view.base_pages or p in view.head.generated_baselines)
        and view.head.generated_baselines.get(p) != view.base_pages.get(p)
    }
    baselines = refresh_baselines(
        view.head, view.base_pages, generated, retain_unchanged=selection is not None
    )
    dependencies = (
        selected_dependencies(kb_dir, view.head, generated, selection)
        if selection is not None
        else {p: tuple(sorted(inputs.values())) for p in generated if p.endswith(".md")}
    )
    if selection is not None:
        from openkb.knowledge_evidence import publication_originals

        originals = publication_originals(
            kb_dir,
            view.scope.view_id,
            inputs.values(),
            dependencies,
            view.scope.wiki_dir,
            generated,
        )
    manifest = KnowledgeRevision(
        knowledge_revision_id=uuid.uuid4().hex,
        view_id=view.scope.view_id,
        base_revision_id=view.head.knowledge_revision_id,
        change_kind="refresh",
        input_revisions=tuple(sorted(inputs.values())),
        page_dependencies=dependencies,
        generated_baselines=baselines,
        original_references=originals,
    )
    proposal = RefreshProposal(
        proposal_id=uuid.uuid4().hex,
        view_id=view.scope.view_id,
        expected_head=view.head,
        expected_kb_generation=view.kb_generation,
        source_generations=generations,
        expected_pages=view.base_pages,
        candidate_pages=generated,
        candidate_manifest=manifest,
        inputs=inputs,
        conflicts=tuple(sorted(protected)),
        selection=selection,
    )
    if not candidate_current(kb_dir, proposal):
        protected.add("sources_or_knowledge_changed")
        proposal = proposal.model_copy(update={"conflicts": tuple(sorted(protected))})
    if not protected:
        return commit_refresh(kb_dir, view.scope.wiki_dir.parent, proposal, check_stop)
    directory = kb_dir / ".openkb/refresh-proposals" / proposal.proposal_id
    schema = catalog_schema_path(kb_dir)
    with mutation_scope(kb_dir, [directory, schema], operation="save-refresh-proposal"):
        copy_tree(view.scope.wiki_dir, directory / "wiki")
        copy_tree(view.scope.wiki_dir.parent / "base", directory / "base")
        if (view.scope.wiki_dir.parent / "index").exists():
            copy_tree(view.scope.wiki_dir.parent / "index", directory / "index")
        write_record(directory / "proposal.json", proposal)
        write_record(schema, CatalogSchema())
        check_stop()
    return RefreshResult(
        "awaiting_confirmation",
        proposal.view_id,
        proposal_id=proposal.proposal_id,
        message="Review protected changes; old knowledge and refresh reasons retained.",
        resources=(directory.relative_to(kb_dir).as_posix(),),
        unfinished=("refresh_review",),
    )


def read_refresh_candidate(kb_dir: Path, identity: str) -> tuple[Path, RefreshProposal]:
    TypeAdapter(RecordId).validate_python(identity)
    directory = kb_dir / ".openkb/refresh-proposals" / identity
    contained_paths(kb_dir, [directory])
    proposal = RefreshProposal.model_validate_json((directory / "proposal.json").read_text())
    if (
        proposal.proposal_id != identity
        or proposal.view_id != proposal.candidate_manifest.view_id
        or proposal.view_id != proposal.expected_head.view_id
        or proposal.candidate_manifest.change_kind != "refresh"
        or proposal.candidate_manifest.base_revision_id
        != proposal.expected_head.knowledge_revision_id
        or tuple(sorted(proposal.inputs.values())) != proposal.candidate_manifest.input_revisions
        or wiki_versions(kb_dir, directory / "wiki") != proposal.candidate_pages
        or wiki_versions(kb_dir, directory / "base") != proposal.expected_pages
    ):
        raise ValueError("Refresh candidate identity or content changed")
    from openkb.knowledge_evidence import validated_input

    originals = set()
    for unit_id, revision_id in proposal.inputs.items():
        revision, frozen = validated_input(kb_dir, revision_id, proposal.view_id)
        if revision.unit_id != unit_id or proposal.expected_head.inputs.get(unit_id) != revision_id:
            raise ValueError("Refresh candidate input does not match its expected head")
        originals.add(frozen.original)
        originals.update(a.artifact for a in frozen.assets if a.artifact)
    manifest = proposal.candidate_manifest
    dependencies = (
        selected_dependencies(
            kb_dir, proposal.expected_head, proposal.candidate_pages, proposal.selection
        )
        if proposal.selection is not None
        else {
            page: manifest.input_revisions
            for page in proposal.candidate_pages
            if page.endswith(".md")
        }
    )
    remaining_refresh(proposal.expected_head, proposal.selection)
    if proposal.selection is not None:
        from openkb.knowledge_evidence import publication_originals
        from openkb.selected_refresh_evidence import validate_selection_manifest

        validate_selection_manifest(proposal)

        originals = set(
            publication_originals(
                kb_dir,
                proposal.view_id,
                proposal.inputs.values(),
                dependencies,
                directory / "wiki",
                proposal.candidate_pages,
            )
        )
    if proposal.selection is not None and candidate_current(kb_dir, proposal):
        from openkb.selected_refresh_evidence import validate_selection

        validate_selection(kb_dir, proposal, directory / "wiki")
    if set(manifest.original_references) != originals or manifest.page_dependencies != dependencies:
        raise ValueError("Refresh candidate evidence does not match its inputs")
    return directory, proposal


def refresh_candidate_view(kb_dir: Path, identity: str) -> dict:
    directory, proposal = read_refresh_candidate(kb_dir, identity)
    chunks = []
    for name in sorted(proposal.expected_pages.keys() | proposal.candidate_pages.keys()):
        if proposal.expected_pages.get(name) == proposal.candidate_pages.get(name):
            continue
        if not name.endswith((".md", ".json", ".txt")):
            chunks.append(f"Asset changed: {name}\n")
            continue
        before, after = directory / "base" / name, directory / "wiki" / name
        chunks.extend(
            difflib.unified_diff(
                before.read_text().splitlines(True) if before.exists() else [],
                after.read_text().splitlines(True) if after.exists() else [],
                fromfile=f"before/{name}",
                tofile=f"after/{name}",
            )
        )
    return {
        "proposal_id": identity,
        "view_id": proposal.view_id,
        "version": HashRegistry.hash_file(directory / "proposal.json"),
        "conflicts": proposal.conflicts,
        "diff": "".join(chunks),
        "status": "awaiting_confirmation" if candidate_current(kb_dir, proposal) else "superseded",
    }
