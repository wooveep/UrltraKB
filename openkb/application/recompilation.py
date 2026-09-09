"""Recompile existing knowledge with one recoverable transaction per document."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openkb.application.execution import ExecutionContext
from openkb.application.file_state import changed_files, contained_paths, file_versions
from openkb.application.removal import _resolve_doc_identifier
from openkb.compilation_report import (
    IncompleteCompilation,
    collect_compile_report,
    require_complete_compilation,
)
from openkb.config import (
    DEFAULT_CONFIG,
    LlmCredentialBundle,
    resolve_concurrency,
    resolve_effective_config,
)
from openkb.locks import (
    LockCancelled,
    async_kb_lock,
    atomic_write_text,
    kb_ingest_lock,
    kb_read_lock,
)
from openkb.log import append_log
from openkb.mutation import RecoveryRequired, mutation_scope
from openkb.processing import ProcessingIncomplete, processing_checkpoint, processing_scope
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
    """Reload the exact document under its lease; never convert or index again.

    Desktop passes an execution context. Legacy adapters keep their own model
    and credential resolution, including REST request overrides.
    """
    kb_dir = kb_dir.resolve()
    async with async_kb_lock(
        kb_dir / ".openkb",
        exclusive=True,
        cancelled=context.cancelled if context else None,
        on_wait=context.waiting if context else None,
    ):
        if version is not None and _version(kb_dir) != version:
            return RecompileResult(
                "conflict",
                message="Knowledge changed after confirmation; review and confirm again",
                unfinished=("compilation",),
            )
        meta = HashRegistry(kb_dir / ".openkb/hashes.json").all_entries().get(file_hash)
        if meta is None:
            return RecompileResult(
                "skipped", message="document is no longer indexed.", version=version
            )
        _validate_metadata(meta)
        name = meta.get("doc_name") or Path(meta.get("name") or "").stem
        kind = "long" if is_long_doc(meta) else "short"
        reason = None
        if not name:
            reason = "registry entry has no doc_name."
        elif not isinstance(name, str) or name in {".", ".."} or any(c in name for c in "/\\\0"):
            reason = "invalid document name in registry."
        doc_id = meta.get("doc_id")
        if reason is None and kind == "long" and not doc_id:
            reason = "legacy long-doc entry without a doc_id; re-add to refresh."
        source = kb_dir / "wiki" / ("summaries" if kind == "long" else "sources") / f"{name}.md"
        if reason is None:
            contained_paths(kb_dir, [source])
            if not source.is_file():
                label = "summary" if kind == "long" else "source"
                reason = f"missing {label} at {source.relative_to(kb_dir)}."
        if reason:
            return RecompileResult("skipped", name, kind, message=reason, version=version)
        paths = contained_paths(
            kb_dir,
            [
                kb_dir / "wiki/summaries" / f"{name}.md",
                kb_dir / "wiki/concepts",
                kb_dir / "wiki/entities",
                kb_dir / "wiki/index.md",
                kb_dir / "wiki/log.md",
            ],
        )
        before = file_versions(kb_dir, paths)
        with context.begin(kb_dir) if context else nullcontext(bundle) as credentials:
            from openkb.agent import compiler

            config = (await asyncio.to_thread(resolve_effective_config, kb_dir))[0]
            if model is None or context:
                model = model or config.get("model", DEFAULT_CONFIG["model"])
                if context and max_concurrency is None:
                    max_concurrency = (
                        resolve_concurrency(config) or compiler.DEFAULT_COMPILE_CONCURRENCY
                    )
            options: dict[str, Any] = {"bundle": credentials}
            if max_concurrency is not None:
                options["max_concurrency"] = max_concurrency
            start = time.monotonic()
            if context:
                context.on_event({"stage": "compiling", "document": name})
            try:
                with (
                    collect_compile_report() as report,
                    processing_scope(config),
                    mutation_scope(kb_dir, paths, operation="recompile"),
                ):
                    if kind == "long":
                        assert isinstance(doc_id, str)  # validated before configuration capture
                        await compiler.compile_long_doc(
                            name, source, doc_id, kb_dir, model, **options
                        )
                    else:
                        await compiler.compile_short_doc(name, source, kb_dir, model, **options)
                    require_complete_compilation()
                    processing_checkpoint()
                    # Compute the receipt before commit. A filesystem error here rolls
                    # back the whole unit; it cannot discard already committed facts.
                    changes = changed_files(kb_dir, paths, before)
                    resources = {
                        str(kb_dir / change.split(": ", 1)[1])
                        for change in changes
                        if not change.startswith("deleted:")
                    }
                    summary = kb_dir / "wiki/summaries" / f"{name}.md"
                    if summary.is_file():
                        resources.add(str(summary))
                    next_version = _version(kb_dir) if version is not None else None
                try:
                    append_log(kb_dir / "wiki", "recompile", f"recompiled {name}")
                except Exception:
                    report.warnings.append("post_commit_log_failed")
            except ProcessingIncomplete as exc:
                return RecompileResult(
                    "unfinished",
                    name,
                    kind,
                    message=exc.reason,
                    unfinished=(exc.stage,),
                    elapsed=time.monotonic() - start,
                    version=version,
                )
            except IncompleteCompilation:
                return RecompileResult(
                    "unfinished",
                    name,
                    kind,
                    message=", ".join(report.quality),
                    unfinished=tuple(report.unfinished),
                    quality=tuple(report.quality),
                    elapsed=time.monotonic() - start,
                    version=version,
                )
            except (LockCancelled, RecoveryRequired):
                raise
            except Exception as exc:
                return RecompileResult(
                    "failed",
                    name,
                    kind,
                    message="Compilation failed",
                    error_type=type(exc).__name__,
                    elapsed=time.monotonic() - start,
                    unfinished=("compilation",),
                    version=version,
                )
            return RecompileResult(
                "compiled",
                name,
                kind,
                elapsed=time.monotonic() - start,
                resources=tuple(sorted(resources)),
                changes=changes,
                version=next_version,
                quality=tuple(report.quality),
                unfinished=tuple(report.unfinished),
                warnings=tuple(report.warnings),
            )
