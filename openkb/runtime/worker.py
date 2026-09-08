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
    CheckKnowledge,
    ContinueConversation,
    DeleteConversation,
    ExportConversation,
    GenerateArtifact,
    GenerateGraph,
    ImportFile,
    ImportUrl,
    RecompileDocument,
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
    if isinstance(request, (DeleteConversation, ExportConversation)):
        from openkb.application.sessions import delete_conversation, export_conversation

        session_result = (
            delete_conversation(root, request.session_id, version=request.version, context=context)
            if isinstance(request, DeleteConversation)
            else export_conversation(root, request.session_id, unique=True, context=context)
        )
        return UnitResult(
            "completed"
            if session_result.status in {"deleted", "exported"}
            else "skipped"
            if session_result.status == "missing"
            else "failed",
            resources=session_result.resources,
            changes=session_result.changes,
            session_id=request.session_id,
            error="Conversation changed; review its latest completed history"
            if session_result.status == "conflict"
            else "Conversation no longer exists; no change was made"
            if session_result.status == "missing"
            else None,
        )
    if isinstance(request, GenerateGraph):
        from openkb.application.artifacts import generate_graph

        graph = generate_graph(root, context=context)
        return UnitResult(
            "completed" if graph.path else "skipped",
            resources=(str(graph.path),) if graph.path else (),
            changes=("updated: output/visualize/graph.html",) if graph.path else (),
            error=None if graph.path else "No wiki pages to visualize",
        )
    context.install_process_settings = True
    if isinstance(request, GenerateArtifact):
        import asyncio

        from openkb.application.generators import GenerationOptions, generate_artifact

        generated = asyncio.run(
            generate_artifact(
                root,
                GenerationOptions(
                    request.target_type,
                    request.name,
                    request.intent,
                    overwrite="archive" if request.replace else "refuse",
                    version=request.version,
                ),
                context=context,
            )
        )
        return UnitResult(
            generated.status if generated.status in {"completed", "blocked"} else "failed",
            resources=generated.resources,
            changes=generated.changes,
            quality=generated.quality,
            unfinished=generated.unfinished,
            error=generated.message,
            halt=generated.status == "blocked",
            output="\n".join([*generated.validation.errors, *generated.validation.warnings])
            if generated.validation
            else "",
        )
    if isinstance(request, CheckKnowledge):
        import asyncio

        from openkb.application.maintenance import LintOptions, check_knowledge

        context.install_process_settings = request.semantic
        checked = asyncio.run(
            check_knowledge(
                root,
                LintOptions(fix=request.fix, semantic=request.semantic, version=request.version),
                context=context,
            )
        )
        return UnitResult(
            "failed" if checked.status == "conflict" else checked.status,
            resources=checked.resources,
            changes=checked.changes,
            quality=checked.quality,
            unfinished=checked.unfinished,
            halt=checked.status == "blocked",
            error="Wiki changed; review the latest link repair plan"
            if checked.status == "conflict"
            else "No documents indexed; semantic checks skipped"
            if checked.status == "skipped"
            else f"Knowledge check failed ({checked.error_type})"
            if checked.error_type
            else None,
        )
    if isinstance(request, RecompileDocument):
        import asyncio

        from openkb.application.recompilation import recompile_document

        recompiled = asyncio.run(
            recompile_document(root, request.file_hash, context=context, version=request.version)
        )
        return UnitResult(
            {"compiled": "completed", "conflict": "failed"}.get(
                recompiled.status, recompiled.status
            ),
            resources=recompiled.resources,
            error=f"{recompiled.message} ({recompiled.error_type})"
            if recompiled.error_type
            else recompiled.message,
            changes=recompiled.changes,
            unfinished=recompiled.unfinished,
            revision=recompiled.version,
            quality=recompiled.quality,
        )
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
            result = import_document(
                root,
                Path(request.source),
                context=context,
                source_root=root / "raw" if request.wait_for_stable else None,
            )
        return UnitResult(
            "completed" if result.status == "added" else result.status,
            resources=result.resources,
            error="Document import failed" if result.status == "failed" else None,
            quality=result.quality,
            unfinished=result.unfinished,
            revision=result.input_version,
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
            resources=answer.resources,
            changes=answer.changes,
            error=answer.error,
            unfinished=answer.unfinished,
            halt=answer.status == "blocked",
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
    from openkb.inputs import InputChanged, preparation_directory
    from openkb.lifecycle import KnowledgeBaseIncomplete, KnowledgeBaseRemoved, expected_generation
    from openkb.locks import LockCancelled
    from openkb.mutation import RecoveryRequired
    from openkb.runtime.input_store import child_preparation, reap_orphaned_inputs

    channel = WorkerChannel(connection, events, identity)
    context = ExecutionContext(
        snapshot=snapshot,
        cancelled=channel.stopped.is_set,
        on_event=channel.event,
        on_snapshot=channel.snapshot,
    )
    try:
        try:
            with (
                child_preparation(prepared_dir),
                preparation_directory(prepared_dir),
                expected_generation(Path(identity.kb_dir), identity.generation),
            ):
                result = _execute(request, identity, context, prepared_dir)
        except WaitingForLease:
            channel.send("deferred")
            return
        except InputChanged:
            if isinstance(request, ImportFile) and request.wait_for_stable:
                # Input preparation has not begun business or fixed new
                # credentials; this wait never retries a completed model call.
                channel.send("deferred", reason="input")
                return
            result = UnitResult("failed", error="Input changed during preparation")
        except LockCancelled:
            result = UnitResult("stopped")
        except (KnowledgeBaseRemoved, KnowledgeBaseIncomplete):
            result = UnitResult(
                "blocked", error="Knowledge base is unavailable; reopen or diagnose it", halt=True
            )
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
                reap_orphaned_inputs(prepared_dir.parents[2])
            except OSError:
                logging.getLogger(__name__).warning("Orphaned task input cleanup failed")
        # Progress is lossy by contract. A full queue must never prevent child
        # exit; results use the receipt and the separately consumed pipe.
        events.cancel_join_thread()
        events.close()
        connection.close()
