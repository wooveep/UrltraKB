"""One spawn child, one execution unit; control is independent of progress."""

from __future__ import annotations

import queue
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from openkb.config_state import ConfigSnapshot
from openkb.runtime.records import UnitIdentity, UnitResult, save_receipt
from openkb.runtime.requests import (
    AskQuestion,
    CheckKnowledge,
    CleanupSourceHistory,
    ConfirmSourcePage,
    ContinueConversation,
    ContinueSource,
    DeleteConversation,
    ExportConversation,
    GenerateArtifact,
    GenerateGraph,
    ImportFile,
    ImportUrl,
    RebuildSourceNavigation,
    RecompileDocument,
    RemoveDocument,
    ReparseSource,
    ReprocessSourcePage,
    SavePage,
    UnitRequest,
)


class WaitingForLease(Exception):
    """No business work began; yield the process slot to independent work."""


class WorkerChannel:
    def __init__(self, connection: Any, events: Any, identity: UnitIdentity) -> None:
        self.connection, self.events, self.identity = connection, events, identity
        self.stopped = threading.Event()
        self.budget_expired = threading.Event()
        self.parent_gone = threading.Event()
        self.acknowledged = threading.Event()
        self.navigation_acknowledged = threading.Event()
        self.sequence = 0
        self._event_lock = threading.Lock()
        self.truncated = False
        self.reader = threading.Thread(target=self._read_control, daemon=True)
        self.reader.start()

    def _read_control(self) -> None:
        try:
            while True:
                message = self.connection.recv()
                if message == "stop":
                    self.stopped.set()
                elif message == "budget":
                    self.budget_expired.set()
                    self.stopped.set()
                elif message == "snapshot-ack":
                    self.acknowledged.set()
                elif message == "navigation-ack":
                    self.navigation_acknowledged.set()
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
        with self._event_lock:
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
    if isinstance(request, RebuildSourceNavigation):
        from openkb.application.source_actions import rebuild_source_navigation

        navigation = rebuild_source_navigation(
            root,
            request.source_id,
            version_id=request.version_id,
            parse_id=request.parse_id,
            context=context,
        )
        reason = navigation["reason"]
        return UnitResult(
            "stopped"
            if reason == "navigation_stopped"
            else "unfinished"
            if navigation["status"] == "degraded"
            else "completed",
            error=reason,
            unfinished=("navigation",) if reason else (),
            changes=("source navigation",),
        )
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
    if isinstance(request, CleanupSourceHistory):
        from openkb.application.source_cleanup import cleanup_history

        cleaned = cleanup_history(root, request.preview_id, context=context)
        return UnitResult(
            "completed",
            changes=(
                f"Cleaned {len(cleaned.files)} unreferenced history files ({cleaned.bytes} bytes)",
            ),
        )
    if isinstance(request, ConfirmSourcePage):
        from openkb.application.source_actions import confirm_source_page

        context.check_stop()
        confirm_source_page(
            root,
            request.source_id,
            version_id=request.version_id,
            parse_id=request.parse_id,
            page=request.page,
            reason=request.reason,
            context=context,
        )
        return UnitResult("completed", changes=(f"Source page {request.page}: {request.reason}",))
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
            warnings=recompiled.warnings,
            document=recompiled.document,
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
    if isinstance(
        request, (ImportFile, ImportUrl, ContinueSource, ReparseSource, ReprocessSourcePage)
    ):
        from openkb.application.documents import import_document

        if isinstance(request, ReprocessSourcePage):
            from openkb.application.source_actions import reprocess_source_page

            result = reprocess_source_page(
                root,
                request.source_id,
                engine=request.engine,
                version_id=request.version_id,
                parse_id=request.parse_id,
                page=request.page,
                acknowledge_unknown=request.acknowledge_unknown,
                context=context,
            )
        elif isinstance(request, ReparseSource):
            from openkb.application.source_actions import reparse_source

            result = reparse_source(
                root, request.source_id, version_id=request.version_id, context=context
            )
        elif isinstance(request, ContinueSource):
            from openkb.application.source_actions import continue_source

            result = continue_source(
                root,
                request.source_id,
                version_id=request.version_id,
                proposal_id=request.proposal_id,
                accept_pages=list(request.accept_pages)
                if request.accept_pages is not None
                else None,
                context=context,
            )
        elif isinstance(request, ImportUrl):
            from openkb.application.urls import import_url

            result = import_url(root, request.url, context=context, prepared_dir=prepared_dir)
        else:
            result = import_document(
                root,
                Path(request.source),
                context=context,
                source_root=root / "raw" if request.wait_for_stable else None,
                source_origin=request.upload_origin,
            )
        unit = UnitResult.from_document(result)
        if isinstance(request, (ReparseSource, ReprocessSourcePage)) and result.stage == "parsed":
            from dataclasses import replace

            # This operation completed its parsing goal; knowledge compilation is
            # a separate action and remains not_started in the document result.
            return replace(unit, status="completed")
        return unit
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
                    new_session_id=request.new_session_id,
                    attempt_id=request.attempt_id,
                    submission_order=request.submission_order,
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
    from openkb.cancellation import OperationCancelled
    from openkb.inputs import InputChanged, preparation_directory
    from openkb.lifecycle import KnowledgeBaseIncomplete, KnowledgeBaseRemoved, expected_generation
    from openkb.locks import LockCancelled
    from openkb.mutation import RecoveryRequired
    from openkb.processing import ProcessingIncomplete
    from openkb.runtime.diagnostics import WorkerDiagnostics
    from openkb.runtime.input_store import child_preparation, reap_orphaned_inputs
    from openkb.runtime.model_cancellation import DocumentCancellation

    channel = WorkerChannel(connection, events, identity)
    context = ExecutionContext(
        snapshot=snapshot,
        cancelled=channel.stopped.is_set,
        on_event=channel.event,
        on_snapshot=channel.snapshot,
    )
    business_result = None

    def committed(document):
        nonlocal business_result
        business_result = UnitResult.from_document(document)
        if isinstance(request, RecompileDocument) and request.version is not None:
            from openkb.application.recompilation import _version

            business_result = replace(business_result, revision=_version(Path(identity.kb_dir)))
        save_receipt(receipt_dir, identity, business_result)
        if not channel.send("navigation"):
            raise OperationCancelled("Parent disappeared after publication")
        while not channel.navigation_acknowledged.wait(0.05):
            if channel.parent_gone.is_set() or channel.stopped.is_set():
                raise OperationCancelled("Stopped after publication")

    context.on_committed = committed
    try:
        try:
            with (
                DocumentCancellation(
                    channel.stopped,
                    enabled=isinstance(
                        request,
                        (
                            ImportFile,
                            ImportUrl,
                            RecompileDocument,
                            ContinueSource,
                            ReparseSource,
                            ReprocessSourcePage,
                            RebuildSourceNavigation,
                        ),
                    ),
                    budget_expired=channel.budget_expired,
                ) as cancellation,
                WorkerDiagnostics(
                    receipt_dir.parent / "logs" / identity.task_id / f"{identity.unit_id}.log",
                    channel.event,
                ) as diagnostics,
                child_preparation(prepared_dir),
                preparation_directory(prepared_dir),
                expected_generation(Path(identity.kb_dir), identity.generation),
            ):
                if snapshot is not None:
                    diagnostics.log.include_secrets(snapshot.values())

                def observed_snapshot(value):
                    diagnostics.log.include_secrets(value.values())
                    channel.snapshot(value)

                def observed_event(value):
                    # These stages precede another expensive operation. A
                    # committed event must still report the completed result.
                    if value.get("stage") in {"converting", "indexing", "compiling"}:
                        cancellation.check()
                    diagnostics.stage(value)

                context.on_snapshot = observed_snapshot
                context.on_event = observed_event
                diagnostics.emit(f"开始第 {int(identity.unit_id) + 1} 项：{type(request).__name__}")
                result = _execute(request, identity, context, prepared_dir)
                business_result = result
                # Persist business facts before auxiliary log/client teardown.
                # The parent still waits for exit and recovery before claiming
                # stop or cleanup confirmation.
                save_receipt(receipt_dir, identity, result)
                channel.send(
                    "result", result=result, truncated=channel.truncated, sequence=channel.sequence
                )
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
        except ProcessingIncomplete as exc:
            result = UnitResult("unfinished", error=exc.reason, unfinished=(exc.stage,))
        except (LockCancelled, OperationCancelled):
            result = (
                UnitResult("unfinished", error="time_budget_exhausted", unfinished=("processing",))
                if channel.budget_expired.is_set()
                else UnitResult("stopped")
            )
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
        if business_result is not None:
            warnings = set(business_result.warnings) | diagnostics.warnings
            if result != business_result:
                warnings.add("worker_cleanup_failed")
            document = business_result.document
            if document is not None:
                document = replace(
                    document, warnings=tuple(sorted(set(document.warnings) | warnings))
                )
            result = replace(business_result, warnings=tuple(sorted(warnings)), document=document)
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
