"""Recompile existing knowledge with one recoverable transaction per document."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.application.documents import DocumentResult
from openkb.application.execution import ExecutionContext
from openkb.application.file_state import changed_files, contained_paths, file_versions
from openkb.application.removal import _resolve_doc_identifier
from openkb.config import (
    LlmCredentialBundle,
    resolve_effective_config,
)
from openkb.locks import (
    atomic_write_text,
    kb_ingest_lock,
    kb_read_lock,
)
from openkb.mutation import mutation_scope
from openkb.state import HashRegistry

LONG_DOC_TYPES = frozenset({"long_pdf"})


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
) -> RecompileSelection:
    if bool(identifier) == all_docs:
        return RecompileSelection("invalid")
    with kb_read_lock(kb_dir / ".openkb"):
        registry = HashRegistry(kb_dir / ".openkb/hashes.json")
        for meta in registry.all_entries().values():
            _validate_metadata(meta)
        matches = (
            list(registry.all_entries().items())
            if all_docs
            else _resolve_doc_identifier(registry, identifier or "")
        )
        targets = tuple(
            RecompileTarget(
                h,
                m.get("name") or "?",
                m.get("doc_name") or m.get("name") or "?",
                "long" if is_long_doc(m) else "short",
            )
            for h, m in matches
        )
        if not targets:
            return RecompileSelection("empty" if all_docs else "not_found")
        if not all_docs and len(targets) > 1:
            return RecompileSelection("multiple", targets)
        return RecompileSelection("ready", targets, _version(kb_dir) if confirmation else None)


def _version(kb_dir: Path) -> str:
    """Version every file the compiler may replace, plus document identities.

    Includes absent targets and newly created files. A successful unit returns
    its resulting version so its own batch can continue without authorizing
    intervening changes made by another operation.
    """
    roots = [
        kb_dir / name
        for name in (
            ".openkb/hashes.json",
            "wiki/summaries",
            "wiki/concepts",
            "wiki/entities",
            "wiki/index.md",
        )
    ]
    values = file_versions(kb_dir, roots)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _validate_metadata(meta: object) -> None:
    if not isinstance(meta, dict) or any(
        meta.get(field) is not None and not isinstance(meta[field], str)
        for field in ("name", "doc_name", "type", "doc_id")
    ):
        raise ValueError("Invalid document registry metadata")


def refresh_schema(kb_dir: Path) -> bool:
    """Atomically keep the legacy .bak and install the current schema."""
    from openkb.schema import AGENTS_MD

    with kb_ingest_lock(kb_dir / ".openkb"):
        current, backup = contained_paths(
            kb_dir, [kb_dir / "wiki/AGENTS.md", kb_dir / "wiki/AGENTS.md.bak"]
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
    status: Literal["compiled", "skipped", "failed", "conflict", "unfinished"]
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
    warnings: tuple[str, ...] = ()
    document: DocumentResult | None = None


async def recompile_document(
    kb_dir: Path,
    file_hash: str,
    *,
    context: ExecutionContext | None = None,
    bundle: LlmCredentialBundle | None = None,
    model: str | None = None,
    max_concurrency: int | None = None,
    version: str | None = None,
) -> RecompileResult:
    """Rebuild from saved input through the same protected document pipeline."""
    return await asyncio.to_thread(
        _recompile_saved,
        kb_dir,
        file_hash,
        context=context,
        bundle=bundle,
        model=model,
        max_concurrency=max_concurrency,
        version=version,
    )


def _recompile_saved(kb_dir, file_hash, *, context, bundle, model, max_concurrency, version):
    from openkb.application.document_pipeline import compile_version
    from openkb.inputs import prepared_input
    from openkb.sources import SourceStore

    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        if version is not None and _version(kb_dir) != version:
            return RecompileResult(
                "conflict",
                message="Knowledge changed; review the current version",
                unfinished=("compilation",),
            )
        meta = HashRegistry(kb_dir / ".openkb/hashes.json").get(file_hash)
        if meta is None:
            return RecompileResult(
                "skipped", message="document is no longer indexed.", version=version
            )
        _validate_metadata(meta)
        name = meta.get("doc_name") or Path(meta.get("name") or "").stem
        if not name or name in {".", ".."} or any(c in name for c in "/\\\0"):
            return RecompileResult("failed", message="Invalid document name")
        kind = "long" if is_long_doc(meta) else "short"
        with context.begin(kb_dir) as credentials:
            settings = resolve_effective_config(kb_dir)[0]
            if model is not None:
                settings["model"] = model
            if max_concurrency is not None:
                settings["processing"] = {**settings["processing"], "concurrency": max_concurrency}
            store = SourceStore(kb_dir)
            if meta.get("source_id"):
                source = store.current(meta["source_id"])
            else:
                candidate = _saved_legacy_input(kb_dir, meta, name)
                if candidate is None:
                    return RecompileResult(
                        "unfinished",
                        name,
                        kind,
                        message="saved_original_missing",
                        unfinished=("source_intake",),
                    )
                with prepared_input(candidate) as ready:
                    source = store.intake(ready)
            start = time.monotonic()
            roots = [kb_dir / "wiki"]
            before = file_versions(kb_dir, roots)
            result = compile_version(
                kb_dir,
                source,
                settings,
                bundle=bundle or credentials,
                on_event=context.on_event,
                force=True,
                document_name=name,
                replaces=file_hash,
            )
            return RecompileResult(
                "compiled" if result.status == "added" else result.status,
                name,
                kind,
                message=result.reason,
                error_type=(
                    result.reason.rsplit(":", 1)[-1]
                    if result.status == "failed" and result.reason and ":" in result.reason
                    else None
                ),
                elapsed=time.monotonic() - start,
                resources=result.resources,
                changes=changed_files(kb_dir, roots, before),
                unfinished=result.unfinished,
                quality=result.quality,
                warnings=result.warnings,
                version=_version(kb_dir) if version is not None else None,
                document=result,
            )


def _saved_legacy_input(kb_dir: Path, meta: dict, name: str) -> Path | None:
    from openkb.inputs import SUPPORTED_EXTENSIONS

    if meta.get("type", "short") not in {
        *[suffix[1:] for suffix in SUPPORTED_EXTENSIONS],
        "short",
        "long_pdf",
    }:
        return None
    candidates = [
        kb_dir / meta.get("raw_path", "raw/" + (meta.get("name") or name)),
        kb_dir / "wiki/sources" / (name + (".json" if is_long_doc(meta) else ".md")),
    ]
    for candidate in contained_paths(kb_dir, candidates):
        if candidate.is_file():
            return candidate
    return None
