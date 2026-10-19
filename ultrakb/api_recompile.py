"""REST event/count compatibility over the shared per-document compiler."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.recompilation import recompile_document, select_recompilation
from ultrakb.application.recompilation import refresh_schema as refresh_kb_schema
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.log import append_log


async def iter_recompile(
    kb_dir: Path,
    doc_name: str | None = None,
    *,
    unit_id: str | None = None,
    all_docs=False,
    dry_run=False,
    refresh_schema=False,
    bundle=None,
    scope: KnowledgeScope | None = None,
):
    selection = await asyncio.to_thread(
        select_recompilation,
        kb_dir,
        doc_name,
        all_docs=all_docs,
        scope=scope,
        unit_id=unit_id,
        confirmation=True,
    )
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
    version = selection.version
    if refresh_schema:
        if scope is not None and scope.knowledge is not None:
            from ultrakb.application.storage_recompilation import refresh_compilation_schema

            scope, version, _ = await asyncio.to_thread(refresh_compilation_schema, scope, version)
        else:
            await asyncio.to_thread(refresh_kb_schema, kb_dir, scope=scope)
    context = ExecutionContext()
    docs = []
    recompiled = skipped = blocked = partial = 0
    final_status = "done"
    continuation_unfinished = ()
    for target in targets:
        result = await recompile_document(
            kb_dir,
            target.file_hash,
            bundle=bundle,
            context=context,
            scope=scope,
            unit_id=target.unit_id,
            version=version,
        )
        from ultrakb.application.storage_recompilation import advance_recompilation

        if result.status == "compiled":
            version = result.version
        doc = {
            "name": result.name or None,
            "doc_name": result.name or None,
            "type": result.kind,
            "status": {"compiled": "ok", "failed": "error"}.get(result.status, result.status),
            "elapsed": round(result.elapsed, 1) if result.elapsed is not None else None,
            "message": result.message,
            "units": [asdict(unit) for unit in result.units],
            "model_usage": result.model_usage,
        }
        if result.error_type and not result.message:
            doc["message"] = f"Compilation failed ({result.error_type})"
        docs.append(doc)
        yield {"event": "doc", **doc}
        if result.status == "compiled":
            recompiled += 1
        elif result.status == "blocked":
            blocked += 1
        elif result.status == "partial":
            partial += 1
        else:
            # Historical REST totals fold ordinary failures into skipped.
            skipped += 1
        if result.status in {"conflict", "stopped"}:
            final_status = "stopped" if result.status == "stopped" else "partial"
            break
        try:
            scope = advance_recompilation(scope, result)
        except Exception:
            final_status = "partial"
            continuation_unfinished = ("confirm next publication",)
            break
    await asyncio.to_thread(
        append_log,
        resolve_scope(kb_dir, scope).wiki_dir,
        "recompile",
        f"recompiled {recompiled}, skipped {skipped}",
        scope=resolve_scope(kb_dir, scope),
    )
    from ultrakb.application.result_usage import merge_result_usage

    usage, unfinished = await asyncio.to_thread(
        merge_result_usage, kb_dir, [doc["model_usage"] for doc in docs]
    )
    yield {
        "event": "final",
        "status": final_status,
        "total": total,
        "recompiled": recompiled,
        "skipped": skipped,
        "blocked": blocked,
        "partial": partial,
        "remaining": total - len(docs),
        "docs": docs,
        "model_usage": usage,
        "unfinished": (*continuation_unfinished, *unfinished),
    }
