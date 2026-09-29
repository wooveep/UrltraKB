"""Recompile existing knowledge with one recoverable transaction per document."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.application.execution import ExecutionContext
from openkb.application.file_state import changed_files, contained_paths, file_versions
from openkb.application.removal import _resolve_doc_identifier
from openkb.config import (
    LlmCredentialBundle,
)
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.locks import (
    async_kb_lock,
    atomic_write_text,
    kb_ingest_lock,
    kb_read_lock,
)
from openkb.mutation import mutation_scope
from openkb.state import HashRegistry

LONG_DOC_TYPES = frozenset({"long_pdf", "pageindex_cloud"})


def is_long_doc(meta: dict) -> bool:
    return meta.get("type") in LONG_DOC_TYPES


@dataclass(frozen=True)
class RecompileTarget:
    file_hash: str
    name: str
    doc_name: str
    kind: str


@dataclass(frozen=True)
class RecompileSelection:
    status: Literal["ready", "invalid", "empty", "not_found", "multiple"]
    targets: tuple[RecompileTarget, ...] = ()
    version: str | None = None


def select_recompilation(
    kb_dir: Path,
    identifier: str | None = None,
    *,
    all_docs: bool = False,
    confirmation: bool = False,
    scope: KnowledgeScope | None = None,
) -> RecompileSelection:
    selected_scope = scope
    scope = resolve_scope(kb_dir, scope)
    if bool(identifier) == all_docs:
        return RecompileSelection("invalid")
    with kb_read_lock(kb_dir / ".openkb"):
        from openkb.application.sources import source_inventory

        admitted = source_inventory(kb_dir, scope=selected_scope)
        mapped = {item["legacy_hash"] for item in admitted if item["legacy_hash"]}
        registry = HashRegistry(kb_dir / ".openkb/hashes.json")
        for meta in registry.all_entries().values():
            _validate_metadata(meta)
        matches = (
            list(registry.all_entries().items())
            if all_docs
            else _resolve_doc_identifier(registry, identifier or "")
        )
        if scope.view_id != "legacy":
            matches = []
        matched_legacy = {file_hash for file_hash, _ in matches}
        targets = tuple(
            RecompileTarget(
                h,
                m.get("name") or "?",
                m.get("doc_name") or m.get("name") or "?",
                "long" if is_long_doc(m) else "short",
            )
            for h, m in matches
            if h not in mapped
        )
        targets += tuple(
            RecompileTarget(
                item["source_id"],
                item["name"],
                item["doc_name"],
                "long" if item["display_type"] == "pageindex" else "short",
            )
            for item in admitted
            if all_docs
            or item["legacy_hash"] in matched_legacy
            or identifier
            in {item["source_id"], item["name"], item["doc_name"], item["legacy_hash"]}
        )
        if not targets:
            return RecompileSelection("empty" if all_docs else "not_found")
        if not all_docs and len(targets) > 1:
            return RecompileSelection("multiple", targets)
        return RecompileSelection(
            "ready", targets, _version(kb_dir, scope=selected_scope) if confirmation else None
        )


def _version(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> str:
    """Version every file the compiler may replace, plus document identities.

    Includes absent targets and newly created files. A successful unit returns
    its resulting version so its own batch can continue without authorizing
    intervening changes made by another operation.
    """
    from openkb.application.views import list_views, view_scope

    scopes = [scope] if scope else [view_scope(kb_dir, view.view_id) for view in list_views(kb_dir)]
    roots = [
        kb_dir / ".openkb/hashes.json",
        kb_dir / ".openkb/catalog",
        *sorted((kb_dir / ".openkb/knowledge").glob("*/head.json")),
    ] + [
        selected.wiki_dir / name
        for selected in scopes
        for name in ("summaries", "concepts", "entities", "index.md")
    ]
    values = file_versions(kb_dir, roots)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _validate_metadata(meta: object) -> None:
    if not isinstance(meta, dict) or any(
        meta.get(field) is not None and not isinstance(meta[field], str)
        for field in ("name", "doc_name", "type", "doc_id")
    ):
        raise ValueError("Invalid document registry metadata")


def refresh_schema(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> bool:
    """Atomically keep the legacy .bak and install the current schema."""
    scope = resolve_scope(kb_dir, scope, writable=True)
    from openkb.schema import AGENTS_MD

    with kb_ingest_lock(kb_dir / ".openkb"):
        current, backup = contained_paths(
            kb_dir, [scope.wiki_dir / "AGENTS.md", scope.wiki_dir / "AGENTS.md.bak"]
        )
        if not current.exists() or current.read_text(encoding="utf-8") == AGENTS_MD:
            return False
        previous = current.read_text(encoding="utf-8")
        with mutation_scope(kb_dir, [current, backup], operation="refresh-schema"):
            atomic_write_text(backup, previous)
            atomic_write_text(current, AGENTS_MD)
        return True


@dataclass(frozen=True)
class RecompileResult:
    status: Literal["compiled", "skipped", "failed", "conflict", "blocked", "stopped"]
    name: str = ""
    kind: str = "short"
    message: str | None = None
    error_type: str | None = None
    elapsed: float | None = None
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    version: str | None = None
    quality: tuple[str, ...] = ()


async def recompile_document(
    kb_dir: Path,
    file_hash: str,
    *,
    context: ExecutionContext | None = None,
    bundle: LlmCredentialBundle | None = None,
    model: str | None = None,
    max_concurrency: int | None = None,
    version: str | None = None,
    scope: KnowledgeScope | None = None,
) -> RecompileResult:
    """Reload the exact document under its lease; never convert or index again.

    Desktop passes an execution context. Legacy adapters keep their own model
    and credential resolution, including REST request overrides.
    """
    requested_scope = scope
    scope = resolve_scope(kb_dir, scope, writable=True)
    kb_dir = kb_dir.resolve()
    async with async_kb_lock(
        kb_dir / ".openkb",
        exclusive=True,
        cancelled=context.cancelled if context else None,
        on_wait=context.waiting if context else None,
    ):
        if version is not None and _version(kb_dir, scope=requested_scope) != version:
            return RecompileResult(
                "conflict",
                message="Knowledge changed after confirmation; review and confirm again",
                unfinished=("compilation",),
            )
        from openkb.application.source_recompilation import recompile_source
        from openkb.legacy_sources import admit_legacy_snapshot
        from openkb.source_catalog import list_sources

        started = time.monotonic()
        registry = HashRegistry(kb_dir / ".openkb/hashes.json")
        meta = registry.get(file_hash)
        admitted = next(
            (
                source
                for source in list_sources(kb_dir)
                if file_hash == source.source_id
                or (source.legacy_hash == file_hash and meta is not None)
            ),
            None,
        )
        from openkb.application.sources import source_inventory

        if requested_scope is not None and (
            (
                admitted is not None
                and not any(
                    item["source_id"] == admitted.source_id
                    for item in source_inventory(kb_dir, scope=requested_scope)
                )
            )
            or (admitted is None and requested_scope.view_id != "legacy")
        ):
            return RecompileResult(
                "blocked", message="Source is not assigned to this knowledge view", version=version
            )
        if admitted is None or admitted.legacy_hash:
            meta = registry.get(admitted.legacy_hash) if admitted and admitted.legacy_hash else meta
            if meta is None and admitted is not None:
                meta = {"name": admitted.name, "doc_name": admitted.doc_name}
            if meta is None:
                return RecompileResult(
                    "skipped", message="document is no longer indexed.", version=version
                )
            _validate_metadata(meta)
            name = meta.get("doc_name") or Path(meta.get("name") or "").stem
            kind = "long" if is_long_doc(meta) else "short"
            if kind == "long" and not meta.get("doc_id"):
                return RecompileResult(
                    "skipped",
                    name,
                    kind,
                    message="legacy long-doc entry without a doc_id; re-add to refresh.",
                    version=version,
                )
            try:
                admitted = admit_legacy_snapshot(
                    kb_dir,
                    admitted.legacy_hash if admitted and admitted.legacy_hash else file_hash,
                    meta,
                )
            except (OSError, ValueError) as exc:
                from openkb.ingest_diagnostics import failure_reason

                return RecompileResult(
                    "blocked",
                    name,
                    kind,
                    message=failure_reason(exc),
                    error_type=type(exc).__name__,
                    version=version,
                )
        from openkb.application.sources import source_view_id
        from openkb.knowledge_scope import live_scope

        scope = requested_scope or live_scope(kb_dir, source_view_id(kb_dir, admitted))
        roots = [scope.wiki_dir]
        before = file_versions(kb_dir, roots)
        result = await recompile_source(
            kb_dir,
            admitted.source_id,
            context=context,
            bundle=bundle,
            model=model,
            max_concurrency=max_concurrency,
            scope=scope,
        )
        status: Literal["compiled", "skipped", "failed", "blocked", "stopped"] = "blocked"
        if result.status == "added":
            status = "compiled"
        elif result.status == "skipped":
            status = "skipped"
        elif result.status == "failed":
            status = "failed"
        elif result.status == "stopped":
            status = "stopped"
        summary = scope.wiki_dir / "summaries" / f"{admitted.doc_name}.md"
        kind = (
            "long"
            if (scope.wiki_dir / "sources" / f"{admitted.doc_name}.json").exists()
            else "short"
        )
        return RecompileResult(
            status,
            admitted.doc_name,
            kind,
            message=result.message,
            error_type=result.units[0].error_type if result.units else None,
            elapsed=time.monotonic() - started,
            resources=((str(summary),) if status == "compiled" and summary.exists() else ())
            + result.resources,
            changes=changed_files(kb_dir, roots, before),
            unfinished=result.unfinished,
            version=_version(kb_dir, scope=requested_scope) if version is not None else None,
            quality=result.quality,
        )
