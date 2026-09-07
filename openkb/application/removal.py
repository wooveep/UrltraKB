"""Shared document removal: stable previews, journaled cleanup, and retryable results."""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.application.execution import ExecutionContext
from openkb.locks import kb_ingest_lock, kb_ingest_lock_held, kb_read_lock
from openkb.log import append_log
from openkb.mutation import RecoveryRequired, mutation_scope
from openkb.state import HashRegistry


def _cleanup_pageindex(
    openkb_dir: Path,
    kb_dir: Path,
    doc_name: str,
    doc_id: str | None,
) -> tuple[bool, str]:
    """Drop a long-doc entry from PageIndex's local SQLite + remove its
    managed files. Returns ``(did_cleanup, message)``.

    No-op (returns ``(False, "no PageIndex state")``) when no
    ``pageindex.db`` exists — short-doc-only KBs never created any.

    Falls back to matching by ``doc_name`` via ``list_documents()`` when
    the registry entry pre-dates PR #51's ``doc_id`` field. Ambiguous
    multi-match cases are skipped with a warning rather than guessed.
    """
    if not (openkb_dir / "pageindex.db").exists():
        return False, "no PageIndex state"

    from openkb.application.local_index import remove_index_document

    return remove_index_document(kb_dir, doc_name, doc_id)


def _resolve_doc_identifier(registry, identifier: str) -> list[tuple[str, dict]]:
    """Find registry entries matching ``identifier``.

    Match precedence (returns immediately on the first non-empty bucket):
      1. Exact match on ``metadata['name']`` (the original filename).
      2. Exact match on ``metadata['doc_name']`` (the slug).
      3. Case-insensitive substring match on either field.

    Returns ``[(file_hash, metadata), ...]``. Callers handle the empty,
    single, and multi-match cases.
    """
    entries = registry.all_entries()
    if identifier in entries:
        return [(identifier, entries[identifier])]

    exact_name = [(h, m) for h, m in entries.items() if m.get("name") == identifier]
    if exact_name:
        return exact_name

    exact_slug = [(h, m) for h, m in entries.items() if m.get("doc_name") == identifier]
    if exact_slug:
        return exact_slug

    needle = identifier.lower()
    fuzzy = [
        (h, m)
        for h, m in entries.items()
        if needle in (m.get("name") or "").lower() or needle in (m.get("doc_name") or "").lower()
    ]
    return fuzzy


@dataclass
class RemoveAction:
    """One planned step surfaced in the remove preview/summary."""

    tag: str
    target: str


@dataclass
class RemovePlan:
    """Structured preview of what ``remove`` will do (no side effects yet).

    Built by ``_build_remove_plan``; consumed by the CLI (for printing the
    preview) and by ``_execute_remove_plan`` (for actually doing it).
    """

    name: str
    doc_name: str
    doc_type: str
    file_hash: str
    actions: list[RemoveAction]
    concept_deletes: list[str]
    entity_deletes: list[str]
    raw_path: Path | None
    cleanup_pageindex: bool
    pageindex_doc_id: str | None
    summary_path: Path
    source_md: Path
    source_json: Path
    images_dir: Path
    kept_raw: Path | None = None


@dataclass
class RemoveResult:
    """Outcome of executing a :class:`RemovePlan`.

    ``status="partial"`` means PageIndex cleanup raised and the registry
    entry was deliberately kept so the user can retry — mirroring the CLI.
    """

    status: Literal["removed", "partial"]
    name: str
    doc_name: str
    actions: list[RemoveAction]
    concepts_deleted: list[str]
    entities_deleted: list[str]
    lint_files_changed: int
    lint_ghosts_removed: int
    pageindex_message: str | None
    pageindex_error: str | None
    message: str
    changes: tuple[str, ...] = ()
    needs_repair: bool = False
    error_type: str | None = None


def _build_remove_plan(
    kb_dir: Path,
    file_hash: str,
    meta: dict,
    *,
    keep_raw: bool,
    keep_empty: bool,
) -> RemovePlan:
    """Scan the KB and predict every file remove will touch (no writes).

    Only frontmatter ``sources:`` membership drives the delete/edit
    classification so the plan reflects what the executor will actually do.
    """
    from openkb.source_refs import scan_affected_pages

    name = meta.get("name", "?")
    doc_name = meta.get("doc_name") or Path(name).stem
    doc_type = meta.get("type", "")
    if (
        not isinstance(doc_name, str)
        or not doc_name
        or any(c in doc_name for c in "/\\")
        or doc_name in {".", ".."}
    ):
        raise ValueError("Invalid document name in registry")
    wiki_dir = kb_dir / "wiki"
    openkb_dir = kb_dir / ".openkb"

    actions: list[RemoveAction] = []

    summary_path = wiki_dir / "summaries" / f"{doc_name}.md"
    if summary_path.exists():
        actions.append(RemoveAction("DELETE", str(summary_path.relative_to(kb_dir))))

    source_md = wiki_dir / "sources" / f"{doc_name}.md"
    source_json = wiki_dir / "sources" / f"{doc_name}.json"
    if source_md.exists():
        actions.append(RemoveAction("DELETE", str(source_md.relative_to(kb_dir))))
    if source_json.exists():
        actions.append(RemoveAction("DELETE", str(source_json.relative_to(kb_dir))))

    # Per-doc extracted-images directory (PDF page images + base64 images
    # from docx/pptx + copied relative refs from .md inputs). Created by
    # openkb.images during ingest, keyed by doc_name.
    images_dir = wiki_dir / "sources" / "images" / doc_name
    if images_dir.is_dir():
        actions.append(
            RemoveAction("DELETE", f"{images_dir.relative_to(kb_dir)}/  (images directory)")
        )

    source_file_marker = f"summaries/{doc_name}.md"
    affected_concepts = scan_affected_pages(wiki_dir / "concepts", source_file_marker)
    concept_deletes = [s for s, r in affected_concepts if r == 0 and not keep_empty]
    concept_edits = [s for s, r in affected_concepts if r > 0 or keep_empty]
    for slug in concept_deletes:
        actions.append(RemoveAction("DELETE", f"wiki/concepts/{slug}.md  (only source: this doc)"))
    for slug in concept_edits:
        actions.append(
            RemoveAction("MODIFY", f"wiki/concepts/{slug}.md  (drop this doc from sources)")
        )

    affected_entities = scan_affected_pages(wiki_dir / "entities", source_file_marker)
    entity_deletes = [s for s, r in affected_entities if r == 0 and not keep_empty]
    entity_edits = [s for s, r in affected_entities if r > 0 or keep_empty]
    for slug in entity_deletes:
        actions.append(RemoveAction("DELETE", f"wiki/entities/{slug}.md  (only source: this doc)"))
    for slug in entity_edits:
        actions.append(
            RemoveAction("MODIFY", f"wiki/entities/{slug}.md  (drop this doc from sources)")
        )

    if (wiki_dir / "index.md").exists():
        actions.append(RemoveAction("MODIFY", "wiki/index.md  (remove Documents entry)"))

    actions.append(RemoveAction("REGISTRY", f"remove hash entry  ({file_hash[:12]}…)"))

    # Long PDFs leave state in PageIndex's local store (`.openkb/pageindex.db`
    # row + `.openkb/files/<collection>/<doc_id>.pdf` + extracted images).
    # Only flag this when both the registry says long_pdf and PageIndex
    # state exists on disk — short-doc-only KBs never created any.
    pageindex_doc_id = meta.get("doc_id")
    cleanup_pageindex = doc_type == "long_pdf" and (openkb_dir / "pageindex.db").exists()
    if cleanup_pageindex:
        if pageindex_doc_id:
            actions.append(RemoveAction("PAGEINDEX", f"delete document ({pageindex_doc_id[:12]}…)"))
        else:
            actions.append(
                RemoveAction("PAGEINDEX", "delete document (lookup by doc_name; legacy entry)")
            )

    # Raw copies are named by doc_name since the collision fix: use the
    # recorded raw_path when present. Only pre-upgrade entries (no
    # raw_path field) fall back to the original filename — a recorded
    # path that no longer exists must NOT fall through, or it could
    # delete a same-named raw file belonging to another document.
    raw_path = kept_raw = None
    if meta.get("origin") != "cloud" and doc_type != "pageindex_cloud":
        raw_dir = kb_dir / "raw"
        if meta.get("raw_path"):
            candidate = kb_dir / meta["raw_path"]
        else:
            candidate = raw_dir / name
        candidate = candidate.resolve()
        if not candidate.is_relative_to(raw_dir.resolve()) or candidate == raw_dir.resolve():
            raise ValueError("Recorded raw path escapes the raw directory")
        if candidate.exists():
            entries = HashRegistry(openkb_dir / "hashes.json").all_entries()
            shared = any(
                other_hash != file_hash
                and other.get("raw_path")
                and (kb_dir / other["raw_path"]).resolve() == candidate
                for other_hash, other in entries.items()
            )
            if keep_raw or shared:
                kept_raw = candidate
                actions.append(RemoveAction("KEEP", str(candidate.relative_to(kb_dir))))
            else:
                raw_path = candidate
                actions.append(RemoveAction("DELETE", str(candidate.relative_to(kb_dir))))

    return RemovePlan(
        name=name,
        doc_name=doc_name,
        doc_type=doc_type,
        file_hash=file_hash,
        actions=actions,
        concept_deletes=concept_deletes,
        entity_deletes=entity_deletes,
        raw_path=raw_path,
        cleanup_pageindex=cleanup_pageindex,
        pageindex_doc_id=pageindex_doc_id,
        summary_path=summary_path,
        source_md=source_md,
        source_json=source_json,
        images_dir=images_dir,
        kept_raw=kept_raw,
    )


def _execute_remove_plan(
    kb_dir: Path,
    plan: RemovePlan,
    registry,
    *,
    keep_empty: bool,
) -> RemoveResult:
    """Carry out a remove plan. Registry write is the commit point.

    Every step before ``registry.remove_by_hash`` is idempotent, so a
    PageIndex failure leaves the entry (with its ``doc_id``) intact for a
    retry. The ``lint --fix`` scope is limited to the pages this remove
    actually touched (modified concept + entity pages ∪ index.md) so the
    sweep doesn't strip pre-existing dangling links in unrelated pages
    (issue #58).
    """
    from openkb.agent.compiler import (
        remove_doc_from_concept_pages,
        remove_doc_from_entity_pages,
        remove_doc_from_index,
    )
    from openkb.lint import fix_broken_links

    wiki_dir = kb_dir / "wiki"
    openkb_dir = kb_dir / ".openkb"
    doc_name = plan.doc_name
    name = plan.name

    if not kb_ingest_lock_held(openkb_dir):
        raise RuntimeError("Document removal requires the KB write lease")
    tracked = _wiki_paths(kb_dir, plan) + _commit_paths(kb_dir, plan)
    before = _file_versions(kb_dir, tracked)
    with mutation_scope(kb_dir, _wiki_paths(kb_dir, plan), operation="remove-wiki"):
        plan.summary_path.unlink(missing_ok=True)
        plan.source_md.unlink(missing_ok=True)
        plan.source_json.unlink(missing_ok=True)
        if plan.images_dir.is_dir():
            shutil.rmtree(plan.images_dir)

        concept_result = remove_doc_from_concept_pages(wiki_dir, doc_name, keep_empty=keep_empty)
        entity_result = remove_doc_from_entity_pages(wiki_dir, doc_name, keep_empty=keep_empty)
        remove_doc_from_index(
            wiki_dir,
            doc_name,
            concept_result["deleted"],
            entity_slugs_deleted=entity_result["deleted"],
        )

        lint_scope: list[Path] = [
            wiki_dir / "concepts" / f"{s}.md" for s in concept_result["modified"]
        ]
        lint_scope += [wiki_dir / "entities" / f"{s}.md" for s in entity_result["modified"]]
        index_md = wiki_dir / "index.md"
        if index_md.exists():
            lint_scope.append(index_md)
        files_changed, ghosts = fix_broken_links(wiki_dir, restrict_to=lint_scope)

    wiki_changes = _changes(
        kb_dir,
        _wiki_paths(kb_dir, plan),
        {
            path: version
            for path, version in before.items()
            if path.startswith("wiki/") and path != "wiki/log.md"
        },
    )
    pageindex_message: str | None = None
    try:
        with mutation_scope(
            kb_dir, _commit_paths(kb_dir, plan), operation="remove-index-and-registry"
        ):
            if plan.cleanup_pageindex:
                _, pageindex_message = _cleanup_pageindex(
                    openkb_dir, kb_dir, doc_name, plan.pageindex_doc_id
                )
            registry.remove_by_hash(plan.file_hash)
            if plan.raw_path is not None:
                plan.raw_path.unlink(missing_ok=True)
            append_log(wiki_dir, "remove", name)
    except Exception as exc:
        logging.getLogger(__name__).debug("Document removal commit traceback:", exc_info=True)
        return RemoveResult(
            status="partial",
            name=name,
            doc_name=doc_name,
            actions=plan.actions,
            concepts_deleted=concept_result["deleted"],
            entities_deleted=entity_result["deleted"],
            lint_files_changed=files_changed,
            lint_ghosts_removed=ghosts,
            pageindex_message=None,
            pageindex_error=str(exc),
            message=(
                f"Cleanup failed: {exc}; registry entry kept, "
                f"re-run `openkb remove {name}` to retry"
            ),
            changes=wiki_changes
            if isinstance(exc, RecoveryRequired)
            else _changes(kb_dir, tracked, before),
            needs_repair=isinstance(exc, RecoveryRequired),
            error_type=type(exc).__name__,
        )
    return RemoveResult(
        status="removed",
        name=name,
        doc_name=doc_name,
        actions=plan.actions,
        concepts_deleted=concept_result["deleted"],
        entities_deleted=entity_result["deleted"],
        lint_files_changed=files_changed,
        lint_ghosts_removed=ghosts,
        pageindex_message=pageindex_message,
        pageindex_error=None,
        message=f"{name} removed from knowledge base.",
        changes=_changes(kb_dir, tracked, before),
    )


def run_remove_for_api(
    kb_dir: Path,
    identifier: str,
    *,
    keep_raw: bool = False,
    keep_empty: bool = False,
    dry_run: bool = False,
) -> dict:
    """Resolve ``identifier`` and run remove under the KB ingest lock.

    Shared entry point for the REST ``/api/v1/remove`` endpoint. Resolve +
    plan + execute all run inside ``kb_ingest_lock`` so concurrent
    add/remove can't interleave (matching the CLI's ``@_with_kb_lock``).

    Returns a dict whose ``status`` is one of ``not_found``, ``multiple``,
    ``dry_run``, ``removed``, ``partial``.
    """
    from openkb.state import HashRegistry

    openkb_dir = kb_dir / ".openkb"
    with kb_ingest_lock(openkb_dir):
        registry = HashRegistry(openkb_dir / "hashes.json")
        matches = _resolve_doc_identifier(registry, identifier)
        if not matches:
            return {"status": "not_found", "identifier": identifier}
        if len(matches) > 1:
            return {
                "status": "multiple",
                "identifier": identifier,
                "candidates": [
                    {"name": m.get("name", "?"), "doc_name": m.get("doc_name", "?")}
                    for _, m in matches
                ],
            }

        file_hash, meta = matches[0]
        plan = _build_remove_plan(
            kb_dir,
            file_hash,
            meta,
            keep_raw=keep_raw,
            keep_empty=keep_empty,
        )
        if dry_run:
            return {
                "status": "dry_run",
                "name": plan.name,
                "doc_name": plan.doc_name,
                "actions": [a.__dict__ for a in plan.actions],
                "concepts_deleted": plan.concept_deletes,
                "entities_deleted": plan.entity_deletes,
            }

        result = _execute_remove_plan(kb_dir, plan, registry, keep_empty=keep_empty)
        return {
            "status": result.status,
            "name": result.name,
            "doc_name": result.doc_name,
            "actions": [a.__dict__ for a in result.actions],
            "concepts_deleted": result.concepts_deleted,
            "entities_deleted": result.entities_deleted,
            "lint_files_changed": result.lint_files_changed,
            "lint_ghosts_removed": result.lint_ghosts_removed,
            "pageindex_message": result.pageindex_message,
            "pageindex_error": result.pageindex_error,
            "message": result.message,
        }


def _contained(kb_dir: Path, paths: list[Path]) -> list[Path]:
    root = kb_dir.resolve()
    for path in paths:
        if not path.resolve().is_relative_to(root) or path.resolve() == root:
            raise ValueError("Removal path escapes the knowledge base")
    return paths


def _wiki_paths(kb_dir: Path, plan: RemovePlan) -> list[Path]:
    return _contained(
        kb_dir,
        [
            plan.summary_path,
            plan.source_md,
            plan.source_json,
            plan.images_dir,
            kb_dir / "wiki/concepts",
            kb_dir / "wiki/entities",
            kb_dir / "wiki/index.md",
        ],
    )


def _commit_paths(kb_dir: Path, plan: RemovePlan) -> list[Path]:
    root = kb_dir / ".openkb"
    paths = [root / "hashes.json", kb_dir / "wiki/log.md"]
    if plan.raw_path is not None:
        paths.append(plan.raw_path)
    if plan.cleanup_pageindex:
        paths += [
            root / name
            for name in (
                "pageindex.db",
                "pageindex.db-wal",
                "pageindex.db-shm",
                "pageindex.db-journal",
                "files",
            )
        ]
    return _contained(kb_dir, paths)


def _plan_version(kb_dir: Path, plan: RemovePlan) -> str:
    digest = hashlib.sha256()
    digest.update(repr(plan).encode())
    for root in sorted(set(_wiki_paths(kb_dir, plan) + _commit_paths(kb_dir, plan))):
        paths = sorted(root.rglob("*")) if root.is_dir() else [root]
        # Include the directory itself so adding its first child changes the view.
        digest.update(str(root.relative_to(kb_dir)).encode())
        for path in paths:
            _contained(kb_dir, [path])
            digest.update(str(path.relative_to(kb_dir)).encode())
            if path.is_file():
                with path.open("rb") as file:
                    for chunk in iter(lambda: file.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                digest.update(b"directory" if path.is_dir() else b"missing")
    return digest.hexdigest()


def _file_versions(kb_dir: Path, roots: list[Path]) -> dict[str, str]:
    versions = {}
    for root in roots:
        for path in sorted(root.rglob("*")) if root.is_dir() else [root]:
            _contained(kb_dir, [path])
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as file:
                    for chunk in iter(lambda: file.read(1024 * 1024), b""):
                        digest.update(chunk)
                versions[path.relative_to(kb_dir).as_posix()] = digest.hexdigest()
    return versions


def _changes(kb_dir: Path, roots: list[Path], before: dict[str, str]) -> tuple[str, ...]:
    after = _file_versions(kb_dir, roots)
    return tuple(
        f"{'deleted' if path not in after else 'updated'}: {path}"
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path)
    )


@dataclass(frozen=True)
class RemovalPreview:
    status: Literal["ready", "not_found", "multiple"]
    version: str = ""
    plan: RemovePlan | None = None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemovalOutcome:
    status: str
    preview: RemovalPreview
    result: RemoveResult | None = None
    retained: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()


def preview_removal(
    kb_dir: Path, identifier: str, *, keep_raw: bool = False, keep_empty: bool = False
) -> RemovalPreview:
    kb_dir = kb_dir.resolve()
    if not identifier.strip():
        raise ValueError("Document identifier is required")
    if not (kb_dir / ".openkb").is_dir():
        raise FileNotFoundError("Knowledge base not found")
    with kb_read_lock(kb_dir / ".openkb"):
        registry = HashRegistry(kb_dir / ".openkb/hashes.json")
        matches = _resolve_doc_identifier(registry, identifier)
        if not matches:
            return RemovalPreview("not_found")
        if len(matches) > 1:
            return RemovalPreview(
                "multiple", candidates=tuple(m.get("name", "?") for _, m in matches)
            )
        file_hash, metadata = matches[0]
        plan = _build_remove_plan(
            kb_dir, file_hash, metadata, keep_raw=keep_raw, keep_empty=keep_empty
        )
        return RemovalPreview("ready", _plan_version(kb_dir, plan), plan)


def remove_document(
    kb_dir: Path,
    identifier: str,
    *,
    keep_raw: bool = False,
    keep_empty: bool = False,
    version: str | None = None,
    context: ExecutionContext | None = None,
) -> RemovalOutcome:
    """Revalidate a confirmed preview before cleaning either durable phase.

    CLI/REST can omit a version because their existing adapters confirm under
    one lease or submit an immediate request. Native confirmation supplies the
    preview version; any changed input requires another explicit confirmation.
    """
    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        preview = preview_removal(kb_dir, identifier, keep_raw=keep_raw, keep_empty=keep_empty)
        if preview.status != "ready":
            return RemovalOutcome(preview.status, preview)
        if version is not None and version != preview.version:
            return RemovalOutcome("conflict", preview)
        assert preview.plan is not None
        with context.begin(kb_dir):
            registry = HashRegistry(kb_dir / ".openkb/hashes.json")
            result = _execute_remove_plan(kb_dir, preview.plan, registry, keep_empty=keep_empty)
        retained_paths = [kb_dir / "wiki/log.md"]
        retained_paths += [
            kb_dir / item.split(": ", 1)[1]
            for item in result.changes
            if item.startswith("updated: ")
        ]
        if preview.plan.kept_raw is not None:
            retained_paths.append(preview.plan.kept_raw)
        if result.status == "partial":
            retained_paths += _commit_paths(kb_dir, preview.plan)
        retained = tuple(
            p.relative_to(kb_dir).as_posix() for p in sorted(set(retained_paths)) if p.exists()
        )
        unfinished: tuple[str, ...] = (
            ("index_and_registry_cleanup",) if result.status == "partial" else ()
        )
        if result.needs_repair:
            unfinished += ("knowledge_base_repair",)
        return RemovalOutcome(
            "blocked" if result.needs_repair else result.status,
            preview,
            result,
            retained,
            unfinished,
        )
