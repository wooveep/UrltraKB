"""REST event/count compatibility over the shared per-document compiler."""

from __future__ import annotations

import asyncio
from pathlib import Path

from openkb.application.recompilation import recompile_document, select_recompilation
from openkb.application.recompilation import refresh_schema as refresh_kb_schema
from openkb.config import DEFAULT_CONFIG, resolve_credential_bundle, resolve_effective_config
from openkb.log import append_log


async def iter_recompile(
    kb_dir: Path,
    doc_name: str | None = None,
    *,
    all_docs=False,
    dry_run=False,
    refresh_schema=False,
    bundle=None,
):
    selection = await asyncio.to_thread(select_recompilation, kb_dir, doc_name, all_docs=all_docs)
    targets = selection.targets
    if selection.status != "ready":
        messages = {
            "invalid": "Specify either a doc_name or all_docs, not both."
            if all_docs
            else "Specify a document name or set all_docs to recompile every doc.",
            "empty": "No documents indexed yet.",
            "not_found": f"No document matching '{doc_name}' found in the KB.",
            "multiple": "doc_name matches multiple documents.",
        }
        event = {
            "event": "error",
            "code": {"invalid": 400, "multiple": 409}.get(selection.status, 404),
            "message": messages[selection.status],
        }
        if selection.status == "multiple":
            event["candidates"] = [{"name": t.name, "doc_name": t.doc_name} for t in targets]
        yield event
        return
    total = len(targets)
    yield {"event": "start", "total": total, "all_docs": all_docs}
    if dry_run:
        yield {
            "event": "plan",
            "targets": [
                {"name": t.doc_name, "doc_name": t.doc_name, "type": t.kind} for t in targets
            ],
            "total": total,
        }
        yield {
            "event": "final",
            "status": "dry_run",
            "total": total,
            "recompiled": 0,
            "skipped": 0,
            "docs": [],
        }
        return
    if refresh_schema:
        await asyncio.to_thread(refresh_kb_schema, kb_dir)
    if bundle is None:
        bundle = await asyncio.to_thread(resolve_credential_bundle, kb_dir)
    config = (await asyncio.to_thread(resolve_effective_config, kb_dir))[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    docs = []
    recompiled = skipped = 0
    for target in targets:
        result = await recompile_document(kb_dir, target.file_hash, bundle=bundle, model=model)
        doc = {
            "name": result.name or None,
            "doc_name": result.name or None,
            "type": result.kind,
            "status": {"compiled": "ok", "failed": "error", "skipped": "skipped"}[result.status],
            "elapsed": round(result.elapsed, 1) if result.elapsed is not None else None,
            "message": result.message,
        }
        if result.error_type:
            doc["message"] = f"Compilation failed ({result.error_type})"
        docs.append(doc)
        yield {"event": "doc", **doc}
        if result.status == "compiled":
            recompiled += 1
        else:
            # Historical REST totals fold ordinary failures into skipped.
            skipped += 1
    await asyncio.to_thread(
        append_log, kb_dir / "wiki", "recompile", f"recompiled {recompiled}, skipped {skipped}"
    )
    yield {
        "event": "final",
        "status": "done",
        "total": total,
        "recompiled": recompiled,
        "skipped": skipped,
        "docs": docs,
    }
