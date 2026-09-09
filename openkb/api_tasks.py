"""REST observation of imports owned by the existing isolated task runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from openkb.api_helpers import _sse as sse
from openkb.api_helpers import require_bearer_token
from openkb.api_uploads import cleanup_uploads
from openkb.runtime.input_store import _lease
from openkb.runtime.records import TERMINAL, TaskView
from openkb.runtime.requests import ImportFile
from openkb.runtime.tasks import TaskManager
from openkb.state import HashRegistry


def task_payload(view: TaskView) -> dict[str, Any]:
    payload = view.summary()
    payload.update(
        task_id=view.id,
        added_count=view.succeeded,
        skipped_count=view.skipped,
        failed_count=view.failed,
        unfinished_count=view.unfinished,
        stop_confirmed=view.state == "stopped" and view.processes_reaped,
    )
    return payload


class ImportTasks:
    def __init__(self, history: Path) -> None:
        history.mkdir(parents=True, exist_ok=True)
        self.owner = _lease(history / "api-owner.lock")
        try:
            self.owner.acquire()
        except Exception as exc:
            raise RuntimeError(
                "An API process already owns this task history; use one API worker"
            ) from exc
        try:
            self.manager = TaskManager(history_dir=history)
        except BaseException:
            self.owner.release()
            raise
        self.observers: set[asyncio.Task] = set()
        self.admissions: set[asyncio.Task] = set()
        self.admission_lock = threading.Lock()

    def submit(
        self, kb: Path, uploads: list[tuple[Path, str]], task_id: str | None
    ) -> tuple[str, bool]:
        task_id = task_id or uuid.uuid4().hex
        binding = hashlib.sha256(
            json.dumps(
                [(name, HashRegistry.hash_file(path)) for path, name in uploads], ensure_ascii=False
            ).encode()
        ).hexdigest()
        with self.admission_lock:
            try:
                previous = self.manager.get(task_id)
            except KeyError:
                previous = None
            accepted = self.manager.submit(
                kb,
                [ImportFile(str(path)) for path, _ in uploads],
                task_id=task_id,
                input_binding=binding,
            )
        return accepted, previous is None

    async def retain(self, task_id: str, uploads: list[tuple[Path, str]]) -> None:
        while True:
            view = self.manager.get(task_id)
            if view.state in TERMINAL and view.processes_reaped:
                await asyncio.to_thread(cleanup_uploads, uploads)
                return
            await asyncio.sleep(0.1)

    async def accept(self, kb: Path, uploads: list[tuple[Path, str]], task_id: str | None) -> str:
        # Accepted work and its inputs outlive HTTP observation, including a
        # disconnect while the admission thread is handing off ownership.
        admission = asyncio.create_task(self._admit(kb, uploads, task_id))
        self.admissions.add(admission)
        admission.add_done_callback(self.admissions.discard)
        return await asyncio.shield(admission)

    async def _admit(self, kb: Path, uploads: list[tuple[Path, str]], task_id: str | None) -> str:
        try:
            accepted, fresh = await asyncio.to_thread(self.submit, kb, uploads, task_id)
        except BaseException:
            cleanup_uploads(uploads)
            raise
        if fresh:
            self.observe(accepted, uploads)
        else:
            cleanup_uploads(uploads)
        return accepted

    def observe(self, task_id: str, uploads: list[tuple[Path, str]]) -> None:
        observer = asyncio.create_task(self.retain(task_id, uploads))
        self.observers.add(observer)
        observer.add_done_callback(self.observers.discard)

    async def result(self, kb: str, task_id: str) -> dict[str, Any]:
        while True:
            view = self.manager.get(task_id)
            if view.state in TERMINAL:
                return add_payload(kb, view)
            await asyncio.sleep(0.1)

    async def events(self, kb: str, task_id: str):
        yield sse("start", {"endpoint": "add", "kb": kb, "task_id": task_id})
        previous = None
        while True:
            view = self.manager.get(task_id)
            payload = task_payload(view)
            if payload != previous:
                yield sse("progress", payload)
                previous = payload
            if view.state in TERMINAL:
                yield sse("result", add_payload(kb, view))
                yield sse("done", {"task_id": task_id})
                return
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        await asyncio.gather(*tuple(self.admissions), return_exceptions=True)
        self.manager.shutdown(stop=True)
        while not await asyncio.to_thread(self.manager.join, 1):
            await asyncio.sleep(0)
        await asyncio.gather(*tuple(self.observers), return_exceptions=True)
        self.owner.release()


def add_payload(kb: str, view: TaskView) -> dict[str, Any]:
    files = []
    for result in view.results:
        document = result.document
        files.append(
            {
                "original_name": Path(document.source).name if document else "",
                "saved_path": document.resources[0] if document and document.resources else None,
                "status": document.status if document else result.status,
                "message": document.reason or document.knowledge_compilation
                if document
                else result.error or result.status,
                "document": asdict(document) if document else None,
            }
        )
    return {"kb": kb, "files": files, **task_payload(view)}


router = APIRouter()


@router.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, request: Request, _: None = Depends(require_bearer_token)):
    try:
        return task_payload(request.app.state.import_tasks.manager.get(task_id))
    except KeyError as exc:
        raise HTTPException(404, "Task not found") from exc


@router.post("/api/v1/tasks/{task_id}/stop")
async def stop_task(task_id: str, request: Request, _: None = Depends(require_bearer_token)):
    try:
        manager = request.app.state.import_tasks.manager
        manager.stop(task_id)
        return task_payload(manager.get(task_id))
    except KeyError as exc:
        raise HTTPException(404, "Task not found") from exc
