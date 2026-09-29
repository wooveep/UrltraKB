"""Publish a whole refreshed view against an exact input and knowledge generation."""

import difflib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import TypeAdapter

from openkb.catalog_schema import CatalogSchema, catalog_schema_path
from openkb.file_state import contained_paths
from openkb.ingest_records import KnowledgeHead, KnowledgeRevision
from openkb.knowledge_scope import live_scope
from openkb.lifecycle import current_generation
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
    )
    schema = catalog_schema_path(kb_dir)
    with mutation_scope(
        kb_dir, [snapshot, live, root / "head.json", schema], operation="publish-refresh"
    ):
        copy_tree(directory / "wiki", snapshot / "wiki")
        if (directory / "index").exists():
            copy_tree(directory / "index", snapshot / "index")
        write_record(snapshot / "manifest.json", manifest)
        copy_tree(directory / "wiki", live)
        for name in proposal.expected_pages.keys() - proposal.candidate_pages.keys():
            (live / name).unlink(missing_ok=True)
        # The lease prevents interleaving, and the final generation fence rejects stopped work.
        check_stop()
        if current_generation(kb_dir) != proposal.expected_kb_generation:
            raise ValueError("Knowledge base generation changed")
        write_record(root / "head.json", head)
        write_record(schema, CatalogSchema())
    return RefreshResult(
        "completed",
        proposal.view_id,
        manifest.knowledge_revision_id,
        resources=(snapshot.relative_to(kb_dir).as_posix(),),
    )


def publish_refresh(
    kb_dir: Path,
    view: CompileView,
    inputs: dict[str, str],
    originals: tuple[str, ...],
    generations: dict[str, int],
    check_stop: Callable[[], None],
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
    baselines = {**view.head.generated_baselines, **generated}
    for name in view.base_pages.keys() - generated.keys():
        baselines.pop(name, None)
    manifest = KnowledgeRevision(
        knowledge_revision_id=uuid.uuid4().hex,
        view_id=view.scope.view_id,
        base_revision_id=view.head.knowledge_revision_id,
        change_kind="refresh",
        input_revisions=tuple(sorted(inputs.values())),
        page_dependencies={
            p: tuple(sorted(inputs.values())) for p in generated if p.endswith(".md")
        },
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
        revision, frozen, _ = validated_input(kb_dir, revision_id, proposal.view_id)
        if revision.unit_id != unit_id or proposal.expected_head.inputs.get(unit_id) != revision_id:
            raise ValueError("Refresh candidate input does not match its expected head")
        originals.add(frozen.original)
        originals.update(a.artifact for a in frozen.assets if a.artifact)
    manifest = proposal.candidate_manifest
    if set(manifest.original_references) != originals or manifest.page_dependencies != {
        page: manifest.input_revisions for page in proposal.candidate_pages if page.endswith(".md")
    }:
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
