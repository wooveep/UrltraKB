"""Document conversion, indexing and knowledge compilation shared by adapters."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openkb.add_coordinator import _cleanup_staging_dirs
from openkb.application.execution import ExecutionContext
from openkb.compilation_report import collect_compile_report
from openkb.config import DEFAULT_CONFIG, resolve_concurrency, resolve_effective_config
from openkb.converter import _registry_path, _sanitize_stem, convert_document
from openkb.ingest_result import ImportUnitOutcome, describe_ingest
from openkb.ingest_result import IngestResult as DocumentResult
from openkb.inputs import InputChanged, PreparedInput, prepared_input, validate_source_root
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.lifecycle import read_lifecycle
from openkb.locks import kb_ingest_lock
from openkb.log import append_log
from openkb.mutation import RecoveryRequired, publish_staged_tree
from openkb.view_records import SourceMetadata

logger = logging.getLogger(__name__)


def _staging_dir_for(kb_dir: Path, file_path: Path) -> Path:
    safe = _sanitize_stem(file_path.stem)
    path = kb_dir / ".openkb" / "staging" / f"add-{safe}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _final_artifact_paths(
    result, kb_dir: Path, *, scope: KnowledgeScope | None = None
) -> tuple[Path | None, Path | None]:
    scope = resolve_scope(kb_dir, scope)
    final_raw = None
    final_source = None
    if result.raw_path is not None:
        final_raw = kb_dir / "raw" / result.raw_path.name
    if result.source_path is not None:
        final_source = scope.wiki_dir / "sources" / result.source_path.name
    return final_raw, final_source


def _snapshot_add_paths(
    kb_dir: Path,
    doc_name: str,
    final_raw: Path | None,
    final_source: Path | None,
    *,
    scope: KnowledgeScope | None = None,
) -> list[Path]:
    # NOTE: .openkb/files (the PageIndex blob store) is intentionally NOT
    # snapshotted here. It is append-only by {doc_id}, and the doc_id is only
    # assigned during indexing (after this snapshot). Eagerly snapshotting the
    # whole tree cost one os.link per existing blob on every add; instead the
    # long-doc add path registers just the new blob via snapshot.track_new()
    # once indexing has run.
    scope = resolve_scope(kb_dir, scope)
    paths = [
        kb_dir / ".openkb" / "hashes.json",
        kb_dir / ".openkb" / "pageindex.db",
        kb_dir / ".openkb" / "pageindex.db-wal",
        kb_dir / ".openkb" / "pageindex.db-shm",
        kb_dir / ".openkb" / "pageindex.db-journal",
        scope.wiki_dir / "summaries" / f"{doc_name}.md",
        scope.wiki_dir / "sources" / f"{doc_name}.json",
        scope.wiki_dir / "sources" / "images" / doc_name,
        scope.wiki_dir / "concepts",
        scope.wiki_dir / "entities",
        scope.wiki_dir / "index.md",
        scope.wiki_dir / "log.md",
    ]
    if final_raw is not None:
        paths.append(final_raw)
    if final_source is not None:
        paths.append(final_source)
    return paths


def _run_compile_with_retry(coro_factory, label: str, *, report=logger.info) -> None:
    report(f"  {label}...")
    for attempt in range(2):
        try:
            asyncio.run(coro_factory())
            return
        except RecoveryRequired:
            raise
        except Exception as exc:
            if attempt == 0:
                report("  Retrying compilation in 2s...")
                time.sleep(2)
            else:
                report(f"  [ERROR] Compilation failed: {exc}")
                logger.debug("Compilation traceback:", exc_info=True)
                raise


def add_single_file(
    file_path: Path,
    kb_dir: Path,
    *,
    stage: bool = True,
    bundle=None,
    report=logger.info,
    on_event: Callable[[dict], None] | None = None,
    prepared: PreparedInput | None = None,
    origin_url: str | None = None,
    scope: KnowledgeScope | None = None,
    metadata: SourceMetadata | None = None,
    download_remote_assets: bool | None = None,
) -> Literal["added", "skipped", "failed", "blocked", "partial", "stopped"]:
    """Compatibility projection of the shared import use case's status."""
    result = import_document(
        kb_dir,
        file_path,
        stage=stage,
        bundle=bundle,
        report=report,
        on_event=on_event,
        prepared=prepared,
        origin_url=origin_url,
        scope=scope,
        metadata=metadata,
        download_remote_assets=download_remote_assets,
    )
    for line in describe_ingest(result):
        report(line)
    return result.status


def _add_single_file_locked(
    file_path: Path,
    kb_dir: Path,
    *,
    stage: bool = True,
    bundle=None,
    report=logger.info,
    on_event: Callable[[dict], None] | None = None,
    prepared: PreparedInput | None = None,
    origin_url: str | None = None,
    scope: KnowledgeScope | None = None,
) -> Literal["added", "skipped", "failed"]:
    """Convert, index, and compile a single document into the knowledge base.

    Steps:
    1. Load config to get the model name.
    2. Convert the document (hash-check; skip if already known).
    3. If long doc: run PageIndex then compile_long_doc.
    4. Else: compile_short_doc.

    Returns:
        ``"added"`` on full success, ``"skipped"`` when the file's hash
        is already in the registry (dedup), or ``"failed"`` when any
        pipeline stage raised. URL-ingest distinguishes these so it can
        unlink the just-downloaded raw file on dedup (it would otherwise
        be an orphan) while preserving it on failure so the user can
        retry without re-downloading.
    """
    scope = resolve_scope(kb_dir, scope, writable=True)
    from openkb.agent.compiler import (
        DEFAULT_COMPILE_CONCURRENCY,
        compile_long_doc,
        compile_short_doc,
    )
    from openkb.state import HashRegistry

    openkb_dir = kb_dir / ".openkb"
    config = resolve_effective_config(kb_dir)[0]
    # The REST API passes a per-KB credential bundle so it never pollutes
    # process-wide state; only the CLI path needs the legacy global setup.
    model: str = config.get("model", DEFAULT_CONFIG["model"])

    staging_dir = _staging_dir_for(kb_dir, file_path) if stage else None

    # 2. Convert document into staging when possible.
    if on_event:
        on_event({"stage": "converting", "source": str(file_path)})
    report(f"Adding: {file_path.name}")
    try:
        if prepared is None:
            result = convert_document(file_path, kb_dir, staging_dir=staging_dir)
        else:
            result = convert_document(file_path, kb_dir, staging_dir=staging_dir, prepared=prepared)
    except Exception as exc:
        report(f"  [ERROR] Conversion failed: {exc}")
        logger.debug("Conversion traceback:", exc_info=True)
        _cleanup_staging_dirs([staging_dir])
        return "failed"

    if result.skipped:
        report(f"  [SKIP] Already in knowledge base: {file_path.name}")
        _cleanup_staging_dirs([staging_dir])
        return "skipped"

    doc_name = result.doc_name or file_path.stem
    index_result = None  # populated only on the long-doc branch

    final_raw, final_source = _final_artifact_paths(result, kb_dir, scope=scope)

    def commit_body(snapshot) -> None:
        nonlocal index_result
        publish_staged_tree(staging_dir, kb_dir)
        if final_raw is not None:
            result.raw_path = final_raw
        if final_source is not None:
            result.source_path = final_source

        if result.is_long_doc:
            if result.raw_path is None:
                raise RuntimeError(f"Converted long document has no raw artifact: {file_path.name}")
            report("  Long document detected — indexing with PageIndex...")
            # PageIndex content-dedups: if the same content is already indexed
            # (e.g. hashes.json and pageindex.db diverged after a remove whose
            # PageIndex cleanup failed), col.add() returns the EXISTING doc_id
            # and writes no new blob. Capture the blob set *before* indexing so
            # we register only blobs THIS add actually created — otherwise
            # rollback would delete a prior document's blob.
            if on_event:
                on_event({"stage": "indexing", "source": str(file_path)})
            files_root = kb_dir / ".openkb" / "files"
            blobs_before = set(files_root.glob("*/*")) if files_root.exists() else set()
            try:
                from openkb.indexer import index_long_document

                index_result = index_long_document(
                    result.raw_path, kb_dir, doc_name=doc_name, scope=scope
                )
            except Exception as exc:
                report(f"  [ERROR] Indexing failed: {exc}")
                logger.debug("Indexing traceback:", exc_info=True)
                raise

            # Register only the newly-created blob artifacts for this doc (the
            # {doc_id} file + its images dir) — the append-only store means the
            # name isn't known until now — so rollback + crash recovery remove
            # exactly this add's blob, never a pre-existing one, instead of
            # snapshotting the whole store up front. The doc_id guard + the
            # blobs_before diff keep a dedup hit (or an unexpected empty doc_id)
            # from registering — and later deleting — existing blobs.
            if index_result.doc_id and files_root.exists():
                snapshot.track_new(
                    [
                        p
                        for p in files_root.glob(f"*/{index_result.doc_id}*")
                        if p not in blobs_before
                    ]
                )

            summary_path = scope.wiki_dir / "summaries" / f"{doc_name}.md"
            if on_event:
                on_event({"stage": "compiling", "source": str(file_path)})
            _run_compile_with_retry(
                lambda: compile_long_doc(
                    doc_name,
                    summary_path,
                    index_result.doc_id,
                    kb_dir,
                    model,
                    doc_description=index_result.description,
                    max_concurrency=resolve_concurrency(config) or DEFAULT_COMPILE_CONCURRENCY,
                    bundle=bundle,
                    scope=scope,
                ),
                label=f"Compiling long doc (doc_id={index_result.doc_id})",
                report=report,
            )
        else:
            if result.source_path is None:
                raise RuntimeError(f"Converted document has no source artifact: {file_path.name}")
            source_path = result.source_path
            if on_event:
                on_event({"stage": "compiling", "source": str(file_path)})
            _run_compile_with_retry(
                lambda: compile_short_doc(
                    doc_name,
                    source_path,
                    kb_dir,
                    model,
                    max_concurrency=resolve_concurrency(config) or DEFAULT_COMPILE_CONCURRENCY,
                    bundle=bundle,
                    scope=scope,
                ),
                label="Compiling short doc",
                report=report,
            )

        # Register hash only after successful compilation.
        if result.file_hash:
            registry = HashRegistry(openkb_dir / "hashes.json")
            doc_type = "long_pdf" if result.is_long_doc else file_path.suffix.lstrip(".")
            meta = {
                "name": file_path.name,
                "doc_name": doc_name,
                "type": doc_type,
                "path": origin_url or result.source_identity or _registry_path(file_path, kb_dir),
            }
            if origin_url:
                meta["origin"] = "url"
            if result.raw_path is not None:
                meta["raw_path"] = _registry_path(result.raw_path, kb_dir)
            if result.source_path is not None:
                meta["source_path"] = _registry_path(result.source_path, kb_dir)
            if index_result is not None:
                meta["doc_id"] = index_result.doc_id
            registry.remove_by_doc_name(doc_name)
            for existing_hash, existing_meta in list(registry.all_entries().items()):
                if (
                    existing_hash != result.file_hash
                    and not existing_meta.get("doc_name")
                    and existing_meta.get("name") == file_path.name
                ):
                    registry.remove_by_hash(existing_hash)
            registry.add(result.file_hash, meta)

    def append_ingest_log() -> None:
        append_log(scope.wiki_dir, "ingest", file_path.name)

    from openkb.add_coordinator import AddMutationPlan, run_add_mutation

    plan = AddMutationPlan(
        operation="add",
        details={
            "file_hash": result.file_hash,
            "name": file_path.name,
            "doc_name": doc_name,
        },
        touched_paths=_snapshot_add_paths(kb_dir, doc_name, final_raw, final_source, scope=scope),
        body=commit_body,
        post_commit_hooks=[append_ingest_log],
        hardlink_dirs={
            scope.wiki_dir / "concepts",
            scope.wiki_dir / "entities",
        },
        staging_dirs=[staging_dir],
    )
    if not run_add_mutation(kb_dir, plan):
        return "failed"
    if on_event:
        on_event({"stage": "committed", "source": str(file_path)})
    report(f"  [OK] {file_path.name} added to knowledge base.")
    return "added"


@dataclass
class AddFileResult:
    """Structured add outcome for the REST API.

    Projects the shared ``import_document`` outcome with
    the original filename and a human-readable message, which the API's
    ``/add`` endpoint surfaces per file in its JSON/SSE response.
    """

    original_name: str
    saved_path: str | None
    status: str
    message: str
    source_id: str | None = None
    source_revision_id: str | None = None
    units: tuple[ImportUnitOutcome, ...] = ()
    discovery_pending: int | None = None
    imports_pending: int | None = None
    quality: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()


def _add_for_api(
    file_path: Path,
    kb_dir: Path,
    *,
    bundle=None,
    source_root: Path | None = None,
    scope: KnowledgeScope | None = None,
    metadata: SourceMetadata | None = None,
    download_remote_assets: bool | None = None,
) -> AddFileResult:
    """Run the locked add pipeline and return a structured result for the API.

    Import preparation, locking and business work belong to ``import_document``.
    On ``skipped`` the upload owner checks whether its raw copy can be discarded.
    """
    result = import_document(
        kb_dir,
        file_path,
        bundle=bundle,
        source_root=source_root,
        scope=scope,
        metadata=metadata,
        download_remote_assets=download_remote_assets,
    )
    status_str = result.status
    if status_str == "skipped":
        message = f"Already in knowledge base: {file_path.name}"
    elif status_str == "failed":
        message = result.message or f"Failed to add: {file_path.name} (see server logs)"
    elif status_str != "added":
        message = result.message or f"Import {status_str}: {file_path.name}"
    else:
        message = f"Added: {file_path.name}"
    return AddFileResult(
        original_name=file_path.name,
        saved_path=str(file_path) if status_str == "added" else None,
        status=status_str,
        message=message,
        source_id=result.source_id,
        source_revision_id=result.source_revision_id,
        units=result.units,
        discovery_pending=result.discovery_pending,
        imports_pending=result.imports_pending,
        quality=result.quality,
        unfinished=result.unfinished,
    )


def import_document(
    kb_dir: Path,
    source: Path,
    *,
    bundle=None,
    on_event: Callable[[dict], None] | None = None,
    context: ExecutionContext | None = None,
    origin_url: str | None = None,
    source_root: Path | None = None,
    stage: bool = True,
    prepared: PreparedInput | None = None,
    report=logger.info,
    scope: KnowledgeScope | None = None,
    metadata: SourceMetadata | None = None,
    download_remote_assets: bool | None = None,
) -> DocumentResult:
    """Process one complete item and report only resources actually retained."""
    if download_remote_assets is not None and type(download_remote_assets) is not bool:
        raise ValueError("download_remote_assets must be a boolean")
    requested_scope = scope
    scope = resolve_scope(kb_dir, scope, writable=True)
    from openkb.state import HashRegistry

    root = kb_dir.expanduser().resolve()
    requested_source = source.expanduser().absolute()
    validate_source_root(requested_source, source_root)
    source = requested_source.resolve()
    if not (root / ".openkb/config.yaml").is_file():
        raise ValueError(f"Not a knowledge base: {root}")
    from contextlib import ExitStack, nullcontext

    if context:
        context.on_event({"stage": "preparing", "source": str(source)})
        context.check_stop()
    with (
        read_lifecycle(
            root,
            cancelled=context.cancelled if context else None,
            on_wait=context.waiting if context else None,
        ),
        ExitStack() as inputs,
    ):
        try:
            ready = (
                prepared if prepared is not None else inputs.enter_context(prepared_input(source))
            )
        except (OSError, UnicodeError, InputChanged) as exc:
            if isinstance(exc, InputChanged) and context is not None:
                raise
            report(f"  [ERROR] Input preparation failed: {exc}")
            retained = (
                (str(source),) if source.is_file() and source.is_relative_to(root / "raw") else ()
            )
            return DocumentResult(str(source), "failed", retained)
        with kb_ingest_lock(
            root / ".openkb",
            cancelled=context.cancelled if context else None,
            on_wait=context.waiting if context else None,
        ):
            validate_source_root(requested_source, source_root)
            try:
                if not ready.is_current():
                    ready = ready.refresh()
            except (OSError, UnicodeError, InputChanged) as exc:
                if isinstance(exc, InputChanged) and context is not None:
                    raise
                report(f"  [ERROR] Input preparation failed: {exc}")
                retained = (
                    (str(source),)
                    if source.is_file() and source.is_relative_to(root / "raw")
                    else ()
                )
                return DocumentResult(str(source), "failed", retained)
            validate_source_root(requested_source, source_root)
            with (
                context.begin(root) if context else nullcontext(bundle) as credentials,
                collect_compile_report() as compilation,
            ):
                from openkb.inputs import FROZEN_SOURCE_EXTENSIONS

                if ready.source.suffix.lower() in FROZEN_SOURCE_EXTENSIONS:
                    from dataclasses import replace

                    from openkb.application.ingestion import import_prepared_source

                    result = import_prepared_source(
                        root,
                        ready,
                        bundle=credentials,
                        context=context,
                        on_event=on_event or (context.on_event if context else None),
                        origin_url=origin_url,
                        report=report,
                        metadata=metadata,
                        scope=requested_scope,
                        download_remote_assets=download_remote_assets,
                    )
                    return replace(
                        result,
                        quality=tuple(compilation.quality),
                        unfinished=result.unfinished + tuple(compilation.unfinished),
                    )
                outcome = _add_single_file_locked(
                    source,
                    root,
                    stage=stage,
                    bundle=credentials,
                    prepared=ready,
                    on_event=on_event or (context.on_event if context else None),
                    origin_url=origin_url,
                    report=report,
                    scope=scope,
                )
            entries = HashRegistry(root / ".openkb/hashes.json")
            meta = entries.get(ready.digest)
            resources = []
            if meta:
                for key in ("raw_path", "source_path"):
                    if meta.get(key):
                        target = root / meta[key]
                        if target.is_file():
                            resources.append(str(target))
                if meta.get("doc_name"):
                    summary = scope.wiki_dir / "summaries" / f"{meta['doc_name']}.md"
                    if summary.is_file():
                        resources.append(str(summary))
            elif source.is_relative_to(root / "raw"):
                resources.append(str(source))
            return DocumentResult(
                str(source),
                outcome,
                tuple(resources),
                tuple(compilation.quality),
                tuple(compilation.unfinished),
                ready.digest,
            )
