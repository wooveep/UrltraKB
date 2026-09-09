"""REST event/count compatibility over the shared per-document compiler."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

from openkb.application.recompilation import refresh_schema as refresh_kb_schema
from openkb.application.recompilation import select_recompilation
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import RecompileDocument
from openkb.sources import content_id


async def iter_recompile(
    kb_dir: Path,
    doc_name: str | None = None,
    *,
    all_docs=False,
    dry_run=False,
    refresh_schema=False,
    bundle=None,
    manager=None,
    task_id=None,
):
    from openkb.api_tasks import task_payload

    selection = await asyncio.to_thread(
        select_recompilation, kb_dir, doc_name, all_docs=all_docs, confirmation=True
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
    if manager is None:
        raise RuntimeError("Recompilation requires the API task service")
    if refresh_schema:
        await asyncio.to_thread(refresh_kb_schema, kb_dir)
        selection = await asyncio.to_thread(
            select_recompilation, kb_dir, doc_name, all_docs=all_docs, confirmation=True
        )
    if selection.version is None:
        raise ValueError("Document selection changed; select again")
    binding = content_id(
        {
            "operation": "recompile",
            "doc_name": doc_name,
            "all_docs": all_docs,
            "refresh_schema": refresh_schema,
        }
    )
    accepted = await asyncio.shield(
        asyncio.to_thread(
            manager.submit,
            kb_dir,
            [
                RecompileDocument(target.file_hash, selection.version)
                for target in selection.targets
            ],
            task_id=task_id,
            input_binding=binding,
        )
    )
    yield {"event": "start", "task_id": accepted, "total": total, "all_docs": all_docs}
    count = 0
    previous = None
    while True:
        view = manager.get(accepted)
        if view.stage != previous:
            yield {"event": "progress", **task_payload(view)}
            previous = view.stage
        for result in view.results[count:]:
            yield {"event": "doc", **_document(result)}
        count = len(view.results)
        if view.state in TERMINAL and view.processes_reaped:
            break
        await asyncio.sleep(0.1)
    yield {
        "event": "final",
        **task_payload(view),
        "status": "done" if view.state == "completed" else view.state,
        "total": total,
        "recompiled": view.succeeded,
        "skipped": sum(result.status == "skipped" for result in view.results),
        "docs": [_document(result) for result in view.results],
    }


def _document(result):
    document = result.document
    return {
        "name": Path(document.source).name if document else None,
        "status": {"completed": "ok", "failed": "error"}.get(result.status, result.status),
        "message": document.reason if document else result.error,
        "document": asdict(document) if document else None,
    }
