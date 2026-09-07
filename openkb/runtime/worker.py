"""One spawn child, one execution unit; control is independent of progress."""

from __future__ import annotations

import queue
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openkb.config_state import ConfigSnapshot
from openkb.runtime.records import UnitIdentity, UnitResult, save_receipt
from openkb.runtime.requests import (
    AskQuestion,
    ContinueConversation,
    ImportFile,
    ImportUrl,
    RemoveDocument,
    SavePage,
    UnitRequest,
)


class WaitingForLease(Exception):
    """No business work began; yield the process slot to independent work."""


class WorkerChannel:
    def __init__(self, connection: Any, events: Any, identity: UnitIdentity) -> None:
        self.connection, self.events, self.identity = connection, events, identity
        self.stopped = threading.Event()
        self.parent_gone = threading.Event()
        self.acknowledged = threading.Event()
        self.sequence = 0
        self.truncated = False
        self.reader = threading.Thread(target=self._read_control, daemon=True)
        self.reader.start()

    def _read_control(self) -> None:
        try:
            while True:
                message = self.connection.recv()
                if message == "stop":
                    self.stopped.set()
                elif message == "snapshot-ack":
                    self.acknowledged.set()
        except (EOFError, OSError):
            self.parent_gone.set()
            self.stopped.set()

    def send(self, kind: str, **values: Any) -> bool:
        try:
            self.connection.send({"identity": asdict(self.identity), "kind": kind, **values})
            return True
        except (EOFError, OSError, BrokenPipeError):
            self.parent_gone.set()
            self.stopped.set()
            return False

    def snapshot(self, value: ConfigSnapshot) -> None:
        from openkb.locks import LockCancelled

        if not self.send("snapshot", snapshot=value):
            raise LockCancelled("Parent disappeared before configuration acknowledgement")
        while not self.acknowledged.wait(0.05):
            if self.parent_gone.is_set():
                raise LockCancelled("Parent disappeared before configuration acknowledgement")

    def event(self, value: dict) -> None:
        if value.get("stage") == "waiting":
            raise WaitingForLease()
        self.sequence += 1
        try:
            self.events.put_nowait(
                {"identity": asdict(self.identity), "sequence": self.sequence, "data": value}
            )
        except queue.Full:
            self.truncated = True


def _execute(
    request: UnitRequest, identity: UnitIdentity, context: Any, prepared_dir: Path | None = None
) -> UnitResult:
    from openkb.application.pages import save_page
    from openkb.locks import kb_ingest_lock

    root = Path(identity.kb_dir)
    if isinstance(request, SavePage):
        with kb_ingest_lock(root / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
            with context.begin(root):
                context.on_event({"stage": "saving"})
                saved = save_page(root, request.path, request.body, version=request.version)
                return UnitResult(
                    "completed" if saved.status == "saved" else "failed",
                    resources=(str(root / "wiki" / f"{saved.page.path}.md"),) if saved.page else (),
                    error=None if saved.status == "saved" else saved.status,
                    output=saved.draft or "",
                    revision=saved.page.version if saved.page else None,
                    page=saved.page if saved.status == "saved" else None,
                )
    context.install_process_settings = True
    if isinstance(request, RemoveDocument):
        from openkb.application.removal import remove_document

        removal = remove_document(
            root,
            request.identifier,
            version=request.version,
            keep_raw=request.keep_raw,
            keep_empty=request.keep_empty,
            context=context,
        )
        removal_result = removal.result
        return UnitResult(
            "completed"
            if removal.status == "removed"
            else "blocked"
            if removal.status == "blocked"
            else "failed",
            resources=tuple(str(root / path) for path in removal.retained),
            error=None
            if removal.status == "removed"
            else (
                f"Cleanup failed ({removal_result.error_type}); "
                "see completed changes and unfinished stages"
                if removal_result
                else f"Document removal: {removal.status}"
            ),
            changes=tuple(removal_result.changes) if removal_result else (),
            unfinished=removal.unfinished,
            halt=removal.status == "blocked",
        )
    if isinstance(request, (ImportFile, ImportUrl)):
        from openkb.application.documents import import_document

        if isinstance(request, ImportUrl):
            from openkb.application.urls import import_url

            result = import_url(root, request.url, context=context, prepared_dir=prepared_dir)
        else:
            result = import_document(root, Path(request.source), context=context)
        return UnitResult(
            "completed" if result.status == "added" else result.status,
            resources=result.resources,
            error="Document import failed" if result.status == "failed" else None,
            quality=result.quality,
            unfinished=result.unfinished,
        )
    if isinstance(request, (AskQuestion, ContinueConversation)):
        import asyncio

        from openkb.application.conversations import ask_question, continue_conversation

        if isinstance(request, AskQuestion):
            answer = asyncio.run(
                ask_question(root, request.question, save=request.save, context=context)
            )
        else:
            answer = asyncio.run(
                continue_conversation(
                    root,
                    request.message,
                    session_id=request.session_id,
                    context=context,
                )
            )
        return UnitResult(
            answer.status,
            resources=(answer.saved_path,) if answer.saved_path else (),
            session_id=answer.session_id,
            turn_count=answer.turn_count,
            output=answer.answer,
            output_state="available",
        )
    raise ValueError("Unsupported task request")


def run_unit(
    request: UnitRequest,
    identity: UnitIdentity,
    snapshot: ConfigSnapshot | None,
    receipt_dir: Path,
    connection: Any,
    events: Any,
    prepared_dir: Path | None = None,
) -> None:
    # Imports stay inside the spawn child. No Qt, Click initialization or SDK
    # configuration runs in the task manager's process.
    from openkb.add_coordinator import DirtyRollbackError
    from openkb.application.execution import ExecutionContext
    from openkb.locks import LockCancelled
    from openkb.mutation import RecoveryRequired

    channel = WorkerChannel(connection, events, identity)
    context = ExecutionContext(
        snapshot=snapshot,
        cancelled=channel.stopped.is_set,
        on_event=channel.event,
        on_snapshot=channel.snapshot,
    )
    try:
        try:
            result = _execute(request, identity, context, prepared_dir)
        except WaitingForLease:
            channel.send("deferred")
            return
        except LockCancelled:
            result = UnitResult("stopped")
        except (RecoveryRequired, DirtyRollbackError):
            result = UnitResult("blocked", error="Knowledge base needs repair", halt=True)
        except Exception as exc:
            # Provider exceptions may contain prompts, URLs, headers or keys.
            # Public summaries carry a category, never an unchecked repr.
            result = UnitResult("failed", error=f"Operation failed ({type(exc).__name__})")
        try:
            save_receipt(receipt_dir, identity, result)
        except Exception:
            channel.send(
                "unconfirmed", result=result, truncated=channel.truncated, sequence=channel.sequence
            )
            return
        channel.send(
            "result", result=result, truncated=channel.truncated, sequence=channel.sequence
        )
    finally:
        if channel.parent_gone.is_set() and prepared_dir is not None:
            import logging
            import shutil

            try:
                if prepared_dir.exists():
                    shutil.rmtree(prepared_dir)
            except OSError:
                logging.getLogger(__name__).warning("Orphaned task input cleanup failed")
        # Progress is lossy by contract. A full queue must never prevent child
        # exit; results use the receipt and the separately consumed pipe.
        events.cancel_join_thread()
        events.close()
        connection.close()
