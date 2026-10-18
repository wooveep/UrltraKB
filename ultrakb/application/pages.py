"""Read committed pages and change them with explicit version preconditions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ultrakb import frontmatter
from ultrakb.knowledge_scope import KnowledgeScope, live_scope, reading_scope, resolve_scope
from ultrakb.locks import kb_ingest_lock
from ultrakb.page_ops import (
    DELETABLE_SECTIONS,
    delete_wiki_page,
    edit_wiki_page,
    page_link_context,
    validate_page_ref,
)

if TYPE_CHECKING:
    from ultrakb.storage.wiki_projection import ProjectionResult


@dataclass(frozen=True)
class Page:
    path: str
    content: str
    body: str
    version: str
    validity: str = "current"
    knowledge_revision_id: str | None = None
    source_revision_ids: tuple[str, ...] = ()
    refresh_reasons: tuple[dict, ...] = ()
    publication_id: str | None = None


@dataclass(frozen=True)
class PageSave:
    status: Literal["saved", "conflict", "not_found"]
    page: Page | None
    draft: str
    ghosts_stripped: tuple[str, ...] = ()
    projection: ProjectionResult | None = None
    candidate_revision_id: str | None = None
    reason_code: str | None = None


def read_page(kb_dir: Path, path: str, *, scope: KnowledgeScope | None = None) -> Page:
    kb_dir = kb_dir.expanduser().resolve()
    if not (kb_dir / ".ultrakb").is_dir():
        raise FileNotFoundError(f"Knowledge base not found: {kb_dir}")
    scope = resolve_scope(kb_dir, scope)
    with reading_scope(scope):
        rel = path if path.endswith(".md") else f"{path}.md"
        from ultrakb.knowledge_scope import read_wiki_bytes

        content = read_wiki_bytes(scope, rel).decode("utf-8")
        from ultrakb.knowledge_evidence import page_evidence

        evidence = page_evidence(scope, rel)
        evidence["publication_id"] = scope.publication_id
    parts = frontmatter.split(content)
    return Page(
        Path(rel).with_suffix("").as_posix(),
        content,
        parts[1] if parts else content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
        **evidence,
    )


def save_page(
    kb_dir: Path,
    path: str,
    body: str,
    *,
    version: str | None = None,
    publication_id: str | None = None,
    scope: KnowledgeScope | None = None,
) -> PageSave:
    """Keep metadata and normalize links; a stale desktop draft is never overwritten.

    Legacy callers may omit the precondition, preserving the existing edit API.
    Desktop callers bind each save to the version returned when opening a page.
    """
    section, stem = validate_page_ref(path)
    scope = resolve_scope(kb_dir, scope)
    publication_id = publication_id or scope.publication_id
    if scope.knowledge is not None:
        from .storage_pages import save_storage_page

        assert publication_id is not None
        with reading_scope(scope):
            return save_storage_page(
                scope, f"{section}/{stem}", body, version=version, publication_id=publication_id
            )
    if scope.publication is not None:
        scope = live_scope(kb_dir, scope.view_id)
    path = f"{section}/{stem}"
    if not (kb_dir / ".ultrakb").is_dir():
        raise FileNotFoundError(f"Knowledge base not found: {kb_dir}")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        try:
            current = read_page(kb_dir, f"{path}.md", scope=scope)
        except FileNotFoundError:
            return PageSave("not_found", None, body)
        public_conflict = False
        if publication_id is not None:
            from ultrakb.published_knowledge import read_publication

            public_conflict = read_publication(kb_dir).publication_id != publication_id
        if public_conflict or (version is not None and version != current.version):
            return PageSave("conflict", current, body)
        result = edit_wiki_page(kb_dir, path, body, scope=scope)
        saved_scope = scope
        if publication_id is not None:
            from ultrakb.application.views import published_scope

            saved_scope = published_scope(kb_dir, scope.view_id)
        return PageSave(
            "saved",
            read_page(kb_dir, f"{path}.md", scope=saved_scope),
            body,
            tuple(result["ghosts_stripped"]),
        )


def delete_page(
    kb_dir: Path,
    path: str,
    *,
    dry_run: bool = False,
    version: str | None = None,
    publication_id: str | None = None,
    scope: KnowledgeScope | None = None,
) -> dict:
    """Confirm one public page, then check both baselines inside its write lease.

    Returned publication_id/version identify the confirmed pre-delete page.
    Legacy serialized writers may omit these optimistic preconditions.
    """
    kb_dir = kb_dir.expanduser().resolve()
    section, stem = validate_page_ref(path, allowed=DELETABLE_SECTIONS)
    path = f"{section}/{stem}"
    if not (kb_dir / ".ultrakb").is_dir():
        raise FileNotFoundError(f"Knowledge base not found: {kb_dir}")
    scope = resolve_scope(kb_dir, scope)
    publication_id = publication_id or scope.publication_id
    if scope.knowledge is not None:
        from .storage_pages import delete_storage_page

        assert publication_id is not None
        with reading_scope(scope):
            return delete_storage_page(
                scope, path, dry_run=dry_run, version=version, publication_id=publication_id
            )
    result = {"target": path, "backlinks": [], "publication_id": publication_id, "version": version}

    if dry_run:
        from ultrakb.application.views import published_scope

        if scope.publication is None and not scope.historical:
            scope = published_scope(kb_dir, scope.view_id, publication_id=publication_id)
        if publication_id is not None and scope.publication_id != publication_id:
            raise ValueError("Public knowledge does not match the requested confirmation")
        with reading_scope(scope):
            try:
                page = read_page(kb_dir, path, scope=scope)
            except FileNotFoundError:
                return {**result, "status": "not_found"}
            links = page_link_context(kb_dir, path, scope=scope)
            return {
                **result,
                "status": "dry_run",
                "publication_id": scope.publication_id,
                "version": page.version,
                "backlinks": links["backlinks"],
            }

    if scope.publication is not None:
        scope = live_scope(kb_dir, scope.view_id)
    scope = resolve_scope(kb_dir, scope, writable=True)
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        if publication_id is not None:
            from ultrakb.published_knowledge import PublicationUnavailable, read_publication

            try:
                public = read_publication(kb_dir, view_id=scope.view_id)
            except PublicationUnavailable:
                return {**result, "status": "conflict"}
            if public.publication_id != publication_id:
                return {**result, "status": "conflict"}
        try:
            page = read_page(kb_dir, path, scope=scope)
        except FileNotFoundError:
            return {**result, "status": "not_found"}
        if version is not None and page.version != version:
            return {**result, "status": "conflict"}
        return {
            **delete_wiki_page(kb_dir, path, scope=scope),
            "publication_id": publication_id,
            "version": page.version,
        }
