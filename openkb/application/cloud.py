"""Import an existing PageIndex Cloud document without modifying its cloud corpus."""

from __future__ import annotations

import hashlib
import logging
from contextlib import nullcontext
from pathlib import Path

from openkb.add_coordinator import AddMutationPlan, DirtyRollbackError, run_add_mutation
from openkb.agent.compiler import DEFAULT_COMPILE_CONCURRENCY, compile_long_doc
from openkb.application.documents import (
    DocumentResult,
    _run_compile_with_retry,
    _snapshot_add_paths,
)
from openkb.application.execution import ExecutionContext
from openkb.compilation_report import collect_compile_report, report_compile_issue
from openkb.config import DEFAULT_CONFIG, resolve_concurrency, resolve_effective_config
from openkb.converter import _registry_path, resolve_doc_name_from_key
from openkb.indexer import _cloud_display_stem, _write_long_doc_artifacts, prepare_cloud_import
from openkb.locks import LockCancelled, kb_ingest_lock
from openkb.log import append_log
from openkb.mutation import RecoveryRequired
from openkb.state import HashRegistry

logger = logging.getLogger(__name__)


def import_cloud(
    kb_dir: Path,
    doc_id: str,
    *,
    context: ExecutionContext | None = None,
    settings: dict | None = None,
    report=logger.info,
) -> DocumentResult:
    """Hold execution permission from cloud acquisition through compilation and commit."""
    if not doc_id.strip():
        raise ValueError("A PageIndex Cloud document ID is required")
    root = kb_dir.expanduser().resolve()
    source = f"pageindex-cloud:{doc_id}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    with kb_ingest_lock(
        root / ".openkb",
        cancelled=context.cancelled if context else None,
        on_wait=context.waiting if context else None,
    ):
        if not (root / ".openkb/config.yaml").is_file():
            raise ValueError(f"Not a knowledge base: {root}")
        registry = HashRegistry(root / ".openkb/hashes.json")
        with collect_compile_report() as compilation:
            if registry.is_known(digest):
                report(f"  [SKIP] Already imported from PageIndex Cloud: {doc_id}")
                status = "skipped"
            else:
                with context.begin(root) if context else nullcontext() as bundle:
                    try:
                        status = _import(root, doc_id, source, digest, bundle, settings, report)
                    except (RecoveryRequired, DirtyRollbackError, LockCancelled):
                        raise
                    except Exception as exc:
                        report(f"  [ERROR] Cloud import failed for {doc_id}: {exc}")
                        logger.debug("Cloud import traceback:", exc_info=True)
                        status = "failed"
        meta = HashRegistry(root / ".openkb/hashes.json").get(digest)
        resources = []
        if meta:
            candidates = []
            source_path, doc_name = meta.get("source_path"), meta.get("doc_name")
            if isinstance(source_path, str) and source_path:
                candidates.append(root / source_path)
            if isinstance(doc_name, str) and doc_name:
                candidates.append(root / "wiki/summaries" / f"{doc_name}.md")
            for path in candidates:
                if path.resolve().is_relative_to(root) and path.is_file():
                    resources.append(str(path))
        return DocumentResult(
            source,
            status,
            tuple(resources),
            tuple(compilation.quality),
            tuple(compilation.unfinished),
            digest,
        )


def _import(root, doc_id, source, digest, bundle, settings, report) -> str:
    config = settings if settings is not None else resolve_effective_config(root)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    report(f"Importing from PageIndex Cloud: {doc_id}")
    cloud = prepare_cloud_import(doc_id, root, source)
    registry = HashRegistry(root / ".openkb/hashes.json")
    stem = _cloud_display_stem(cloud.cloud_name, doc_id)
    doc_name = resolve_doc_name_from_key(stem, source, registry)

    def commit_body(_snapshot) -> None:
        summary_path = _write_long_doc_artifacts(
            cloud.tree,
            cloud.all_pages,
            doc_name,
            doc_id,
            root,
            description=cloud.description,
        )
        _run_compile_with_retry(
            lambda: compile_long_doc(
                doc_name,
                summary_path,
                doc_id,
                root,
                model,
                doc_description=cloud.description,
                max_concurrency=resolve_concurrency(config) or DEFAULT_COMPILE_CONCURRENCY,
                bundle=bundle,
            ),
            label=f"Compiling imported doc (doc_id={doc_id})",
            report=report,
        )
        entries = HashRegistry(root / ".openkb/hashes.json")
        entries.remove_by_doc_name(doc_name)
        entries.add(
            digest,
            {
                "name": cloud.cloud_name,
                "doc_name": doc_name,
                "type": "pageindex_cloud",
                "origin": "cloud",
                "path": source,
                "source_path": _registry_path(root / "wiki/sources" / f"{doc_name}.json", root),
                "doc_id": doc_id,
            },
        )

    def append_cloud_log() -> None:
        try:
            append_log(root / "wiki", "ingest", doc_name)
        except Exception:
            report_compile_issue("ingest-log-write-failed", "ingest-log")
            raise  # Preserve the coordinator's existing diagnostic and added status.

    plan = AddMutationPlan(
        operation="cloud_import",
        details={"doc_id": doc_id, "doc_name": doc_name},
        touched_paths=_snapshot_add_paths(root, doc_name, None, None),
        body=commit_body,
        post_commit_hooks=[append_cloud_log],
        hardlink_dirs={root / "wiki/concepts", root / "wiki/entities"},
    )
    if not run_add_mutation(root, plan):
        return "failed"
    report(f"  [OK] {doc_name} imported from PageIndex Cloud.")
    return "added"
