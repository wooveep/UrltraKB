"""Optimistic page edits against an explicitly selected immutable publication."""

import hashlib

from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.lint import _normalize_target, wiki_targets
from ultrakb.page_ops import delete_page_files, edited_page_content, pages_linking_to
from ultrakb.storage.knowledge_store import BoundKnowledge

from .pages import Page, PageSave, read_page
from .publications import publish_knowledge_changes
from .views import storage_scope


def _latest(scope: KnowledgeScope) -> KnowledgeScope:
    assert scope.knowledge is not None
    return storage_scope(scope.kb_dir, scope.knowledge.identity, view_id=scope.view_id)


def _page(scope: KnowledgeScope, path: str) -> Page | None:
    try:
        return read_page(scope.kb_dir, path, scope=scope)
    except FileNotFoundError:
        return None


def save_storage_page(
    scope: KnowledgeScope, path: str, body: str, *, version: str | None, publication_id: str
) -> PageSave:
    latest = _latest(scope)
    current = _page(latest, path)
    if (
        publication_id != scope.publication_id
        or latest.publication_id != publication_id
        or current is not None
        and version is not None
        and current.version != version
    ):
        return PageSave("conflict", current, body)
    if current is None:
        return PageSave("not_found", None, body)
    bound = scope.knowledge
    assert bound is not None
    updated, ghosts = edited_page_content(current.content, body, wiki_targets(bound.files()))
    outcome = publish_knowledge_changes(bound, {f"{path}.md": updated.encode("utf-8")})
    if outcome.status == "needs_review":
        return PageSave(
            "conflict",
            _page(_latest(scope), path),
            body,
            candidate_revision_id=outcome.candidate_revision_id,
            reason_code=outcome.reason_code,
        )
    assert outcome.publication is not None
    saved_scope = storage_scope(
        scope.kb_dir,
        bound.identity,
        publication_id=outcome.publication.publication_id,
        view_id=scope.view_id,
    )
    return PageSave(
        "saved",
        read_page(scope.kb_dir, path, scope=saved_scope),
        body,
        tuple(ghosts),
        outcome.projection,
    )


def _deletion(
    bound: BoundKnowledge, path: str, *, dry_run: bool
) -> tuple[dict[str, bytes | None], dict]:
    with bound.compile_workspace() as baseline:
        wiki = baseline.directory
        page = wiki / f"{path}.md"
        backlinks = pages_linking_to(wiki, _normalize_target(path), exclude=page)
        if dry_run:
            return {}, {"backlinks": backlinks}
        section, stem = path.split("/")
        changed, ghosts = delete_page_files(wiki, section, stem, backlinks)
        changes: dict[str, bytes | None] = {}
        for relative, digest in baseline.files.items():
            if not relative.endswith(".md"):
                continue
            file = wiki / relative
            if not file.is_file():
                changes[relative] = None
            else:
                content = file.read_bytes()
                if hashlib.sha256(content).hexdigest() != digest:
                    changes[relative] = content
        return changes, {
            "backlinks": backlinks,
            "files_changed": changed,
            "ghosts_stripped": ghosts,
        }


def delete_storage_page(
    scope: KnowledgeScope, path: str, *, dry_run: bool, version: str | None, publication_id: str
) -> dict:
    assert scope.knowledge is not None
    result = {"target": path, "backlinks": [], "publication_id": publication_id, "version": version}
    if dry_run:
        if scope.publication_id != publication_id:
            raise ValueError("Public knowledge does not match the requested confirmation")
        current = _page(scope, path)
    else:
        latest = _latest(scope)
        if latest.publication_id != publication_id or scope.publication_id != publication_id:
            return {**result, "status": "conflict"}
        current = _page(scope, path)
    if current is None:
        return {**result, "status": "not_found"}
    result["version"] = current.version
    if not dry_run and version is not None and version != current.version:
        return {**result, "status": "conflict"}
    changes, details = _deletion(scope.knowledge, path, dry_run=dry_run)
    if dry_run:
        return {**result, **details, "status": "dry_run"}
    outcome = publish_knowledge_changes(scope.knowledge, changes)
    if outcome.status == "needs_review":
        return {
            **result,
            **details,
            "status": "conflict",
            "candidate_revision_id": outcome.candidate_revision_id,
            "reason_code": outcome.reason_code,
        }
    assert outcome.publication is not None
    return {
        **result,
        **details,
        "status": "deleted",
        "projection": outcome.projection,
        "result_publication_id": outcome.publication.publication_id,
    }
