"""One spawn child, one execution unit; control is independent of progress."""

from __future__ import annotations

import queue
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ultrakb.config_state import ConfigSnapshot
from ultrakb.runtime.records import UnitIdentity, UnitResult, save_receipt
from ultrakb.runtime.requests import (
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
    RemoveDocuments,
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
        self.event_guard = threading.Lock()
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
        from ultrakb.locks import LockCancelled

        if not self.send("snapshot", snapshot=value):
            raise LockCancelled("Parent disappeared before configuration acknowledgement")
        while not self.acknowledged.wait(0.05):
            if self.parent_gone.is_set():
                raise LockCancelled("Parent disappeared before configuration acknowledgement")

    def event(self, value: dict) -> None:
        if value.get("stage") == "waiting":
            raise WaitingForLease()
        with self.event_guard:
            self.sequence += 1
            try:
                self.events.put_nowait(
                    {"identity": asdict(self.identity), "sequence": self.sequence, "data": value}
                )
            except queue.Full:
                self.truncated = True


def _import_status(status: str) -> str:
    return {
        "added": "completed",
        "compiled": "completed",
        "conflict": "failed",
        "rejected": "failed",
    }.get(status, status)


def _execute(
    request: UnitRequest, identity: UnitIdentity, context: Any, prepared_dir: Path | None = None
) -> UnitResult:
    from ultrakb.application.pages import save_page
    from ultrakb.application.views import view_scope
    from ultrakb.knowledge_scope import resolve_scope
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.runtime.requests import ViewSelection

    root = Path(identity.kb_dir)
    context.usage_task_id = getattr(identity, "task_id", None)
    scope = (
        view_scope(root, request.view_id)
        if isinstance(request, ViewSelection) and request.view_id is not None
        else None
    )
    from ultrakb.runtime.requests import (
        AcceptProposal,
        AcceptRefreshProposal,
        ConfirmEmptySource,
        RefreshKnowledge,
        ReprocessSource,
        RetrySource,
        RetryWorksheet,
        RunPendingJob,
    )

    if isinstance(request, RemoveDocuments):
        from ultrakb.application.batch_removal import BatchRemovalPreview, execute_batch_removal

        reviewed = BatchRemovalPreview.decode(request.preview)
        if reviewed.kb_dir != str(root.resolve()) or reviewed.view_id != (
            request.view_id or "legacy"
        ):
            raise ValueError("Reviewed removal belongs to another knowledge base or view")
        removed = execute_batch_removal(
            reviewed, context=context, confirmed_groups=request.confirmed_groups
        )
        return UnitResult(
            removed.status,
            removal_items=removed.items,
            error=removed.message,
            resources=tuple(
                str(root / path)
                for path in sorted({path for item in removed.items for path in item.retained})
            ),
            revision=removed.publication_id,
            halt=removed.status in {"blocked", "partial"},
            unfinished=("knowledge_base_repair",)
            if removed.needs_repair
            else ("review_removal_again",)
            if removed.status != "completed"
            else (),
        )

    if isinstance(request, RunPendingJob):
        from ultrakb.application.pending import run_pending_job

        context.install_process_settings = True
        outcome = run_pending_job(root, request.job_id, request.dispatch_id, context=context)
        status = outcome["status"]
        return UnitResult(
            "skipped"
            if status == "not_imported"
            else status
            if status in {"completed", "skipped", "partial", "failed", "stopped"}
            else "blocked",
            error=outcome.get("message"),
            quality=tuple(outcome.get("quality", [])),
            unfinished=tuple(outcome.get("unfinished", [])),
            quality_known=outcome.get("quality_known"),
            model_usage=outcome.get("model_usage"),
            changes=(f"Pending job: {request.job_id}; {status}",),
        )

    if isinstance(request, (RetrySource, RetryWorksheet, ReprocessSource)):
        from ultrakb.application.reprocessing import ReprocessingConflict, reprocess_source
        from ultrakb.application.source_retry import RetryTargetChanged, retry_source
        from ultrakb.application.workbook_actions import retry_worksheet
        from ultrakb.ingest_result import describe_ingest

        context.install_process_settings = True
        try:
            result = (
                reprocess_source(
                    root, request.source_id, version=request.version, context=context, scope=scope
                )
                if isinstance(request, ReprocessSource)
                else retry_source(
                    root,
                    request.source_id,
                    context=context,
                    scope=scope,
                    source_revision_id=request.source_revision_id,
                    unit_revision_id=request.unit_revision_id,
                    retry_failed_response=request.retry_failed_response,
                )
                if isinstance(request, RetrySource)
                else retry_worksheet(
                    root,
                    request.source_id,
                    request.unit_id,
                    context=context,
                    scope=scope,
                    source_revision_id=request.source_revision_id,
                    unit_revision_id=request.unit_revision_id,
                    retry_failed_response=request.retry_failed_response,
                )
            )
        except (ReprocessingConflict, RetryTargetChanged) as exc:
            return UnitResult("blocked", error=str(exc), unfinished=("reprocessing_preview",))
        return UnitResult(
            _import_status(result.status),
            resources=result.resources,
            error=result.message,
            changes=describe_ingest(result),
            import_units=result.units,
            quality=result.quality,
            model_usage=result.model_usage,
            unfinished=result.unfinished,
            revision=result.input_version,
        )

    if isinstance(request, (AcceptRefreshProposal, ConfirmEmptySource, RefreshKnowledge)):
        import asyncio

        from ultrakb.application.refresh import (
            accept_refresh_proposal,
            confirm_empty_source,
            refresh_knowledge_view,
        )

        context.install_process_settings = True
        if isinstance(request, AcceptRefreshProposal):
            refreshed = accept_refresh_proposal(
                root, request.proposal_id, version=request.version, scope=scope, context=context
            )
        elif isinstance(request, ConfirmEmptySource):
            refreshed = confirm_empty_source(
                root, request.source_id, generation=request.generation, scope=scope, context=context
            )
        else:
            from ultrakb.application.refresh import refresh_selected_sources

            refreshed = asyncio.run(
                refresh_selected_sources(
                    root,
                    list(request.source_ids),
                    scope=resolve_scope(root, scope),
                    context=context,
                    page_paths=list(request.page_paths) if request.page_paths is not None else None,
                )
                if request.source_ids
                else refresh_knowledge_view(root, scope=resolve_scope(root, scope), context=context)
            )
        return UnitResult(
            refreshed.status
            if refreshed.status in {"completed", "partial", "failed", "stopped"}
            else "blocked",
            resources=tuple(str(root / path) for path in refreshed.resources),
            error=refreshed.message or None,
            unfinished=refreshed.unfinished,
            changes=(refreshed.message,) if refreshed.message else (),
            revision=refreshed.publication_id,
        )

    if isinstance(request, AcceptProposal):
        from ultrakb.application.proposals import accept_proposal
        from ultrakb.ingest_result import describe_ingest

        accepted = accept_proposal(
            root, request.proposal_id, version=request.version, context=context, scope=scope
        )
        return UnitResult(
            "completed" if accepted.status == "added" else accepted.status,
            resources=accepted.resources,
            error=accepted.message,
            changes=describe_ingest(accepted),
            import_units=accepted.units,
            unfinished=accepted.unfinished,
        )
    if isinstance(request, SavePage):
        with kb_ingest_lock(
            root / ".ultrakb", cancelled=context.cancelled, on_wait=context.waiting
        ):
            with context.begin(root):
                context.on_event({"stage": "saving"})
                saved = save_page(
                    root,
                    request.path,
                    request.body,
                    version=request.version,
                    scope=scope,
                    publication_id=request.publication_id,
                )
                return UnitResult(
                    "completed" if saved.status == "saved" else "failed",
                    resources=(str(resolve_scope(root, scope).wiki_dir / f"{saved.page.path}.md"),)
                    if saved.page
                    else (),
                    error=None if saved.status == "saved" else saved.status,
                    output=saved.draft or "",
                    revision=saved.page.version if saved.page else None,
                    page=saved.page if saved.status == "saved" else None,
                )
    if isinstance(request, (DeleteConversation, ExportConversation)):
        from ultrakb.application.sessions import delete_conversation, export_conversation

        session_result = (
            delete_conversation(
                root,
                request.session_id,
                version=request.version,
                view_id=request.view_id,
                context=context,
            )
            if isinstance(request, DeleteConversation)
            else export_conversation(
                root, request.session_id, unique=True, context=context, scope=scope
            )
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
        from ultrakb.application.artifacts import generate_graph

        graph = generate_graph(root, context=context, scope=scope)
        return UnitResult(
            "completed" if graph.path else "skipped",
            resources=(str(graph.path),) if graph.path else (),
            changes=("updated: output/visualize/graph.html",) if graph.path else (),
            error=None if graph.path else "No wiki pages to visualize",
        )
    context.install_process_settings = True
    if isinstance(request, GenerateArtifact):
        import asyncio

        from ultrakb.application.generators import GenerationOptions, generate_artifact

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
                scope=scope,
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

        from ultrakb.application.maintenance import LintOptions, check_knowledge

        context.install_process_settings = request.semantic
        checked = asyncio.run(
            check_knowledge(
                root,
                LintOptions(fix=request.fix, semantic=request.semantic, version=request.version),
                context=context,
                scope=scope,
            )
        )
        return UnitResult(
            "failed" if checked.status == "conflict" else checked.status,
            revision=checked.publication_id,
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

        from ultrakb.application.recompilation import recompile_document

        recompiled = asyncio.run(
            recompile_document(
                root,
                request.file_hash,
                context=context,
                version=request.version,
                scope=scope,
                unit_id=request.unit_id,
            )
        )
        from ultrakb.ingest_result import describe_units

        return UnitResult(
            _import_status(recompiled.status),
            resources=recompiled.resources,
            error=f"{recompiled.message} ({recompiled.error_type})"
            if recompiled.error_type
            else recompiled.message,
            changes=recompiled.changes + describe_units(recompiled.units),
            import_units=recompiled.units,
            unfinished=recompiled.unfinished,
            revision=recompiled.version,
            quality=recompiled.quality,
            quality_known=recompiled.quality_known,
            model_usage=getattr(recompiled, "model_usage", None),
        )
    if isinstance(request, RemoveDocument):
        from ultrakb.application.removal import remove_document

        removal = remove_document(
            root,
            request.identifier,
            version=request.version,
            keep_raw=request.keep_raw,
            keep_empty=request.keep_empty,
            context=context,
            scope=scope,
        )
        removal_result = removal.result
        return UnitResult(
            "completed"
            if removal.status == "removed"
            else "stopped"
            if removal.status == "stopped"
            else "blocked"
            if removal.status == "blocked"
            else "failed",
            resources=tuple(str(root / path) for path in removal.retained),
            error=None
            if removal.status == "removed"
            else removal.message
            if removal.message
            else (
                f"Cleanup failed ({removal_result.error_type}); "
                "see completed changes and unfinished stages"
                if removal_result
                else f"Document removal: {removal.status}"
            ),
            changes=tuple(removal_result.changes) if removal_result else (),
            revision=removal.publication_id,
            unfinished=removal.unfinished,
            halt=removal.status == "blocked",
        )
    if isinstance(request, (ImportFile, ImportUrl)):
        from ultrakb.application.documents import import_document
        from ultrakb.ingest_result import describe_ingest

        if isinstance(request, ImportUrl):
            from ultrakb.application.urls import import_url

            result = import_url(
                root,
                request.url,
                context=context,
                prepared_dir=prepared_dir,
                scope=scope,
            )
        else:
            result = import_document(
                root,
                Path(request.source),
                context=context,
                scope=scope,
                source_root=root / "raw" if request.wait_for_stable else None,
                download_remote_assets=request.download_remote_assets,
            )
        return UnitResult(
            _import_status(result.status),
            resources=result.resources,
            error=result.message
            or ("Document import failed" if result.status == "failed" else None),
            quality=result.quality,
            model_usage=result.model_usage,
            unfinished=result.unfinished,
            revision=result.input_version,
            changes=describe_ingest(result),
            import_units=result.units,
        )
    if isinstance(request, (AskQuestion, ContinueConversation)):
        import asyncio

        from ultrakb.application.conversations import ask_question, continue_conversation

        if isinstance(request, AskQuestion):
            answer = asyncio.run(
                ask_question(
                    root, request.question, save=request.save, context=context, scope=scope
                )
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
                    focus=request.focus,
                    context=context,
                    scope=scope,
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
            answer_outcome=answer.answer_outcome,
            usage=answer.usage,
            model_usage=answer.model_usage,
            context=answer.context,
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
    retry_of: str | None = None,
    require_verified_connection: bool = False,
    config_location: tuple[Path, Path] | None = None,
) -> None:
    # Imports stay inside the spawn child. No Qt, Click initialization or SDK
    # configuration runs in the task manager's process.
    from ultrakb.add_coordinator import DirtyRollbackError
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.model_connections import ConnectionRequired
    from ultrakb.inputs import InputChanged, preparation_directory
    from ultrakb.lifecycle import KnowledgeBaseIncomplete, KnowledgeBaseRemoved, expected_generation
    from ultrakb.llm_execution import ModelBudgetExceeded, ModelDeadlineExceeded
    from ultrakb.locks import LockCancelled
    from ultrakb.mutation import RecoveryRequired
    from ultrakb.runtime.input_store import child_preparation, reap_orphaned_inputs

    if config_location is not None:
        from ultrakb import config

        config.GLOBAL_CONFIG_DIR, config.GLOBAL_CONFIG_PATH = config_location
    channel = WorkerChannel(connection, events, identity)
    from ultrakb.runtime.requests import requires_model

    context = ExecutionContext(
        snapshot=snapshot,
        cancelled=channel.stopped.is_set,
        on_event=channel.event,
        on_snapshot=channel.snapshot,
        parent_usage_task_id=retry_of,
        require_verified_connection=require_verified_connection and requires_model(request),
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
        except (ModelBudgetExceeded, ModelDeadlineExceeded) as exc:
            result = UnitResult("stopped", error=f"Task stopped ({type(exc).__name__})", halt=True)
        except LockCancelled:
            result = UnitResult("stopped")
        except (KnowledgeBaseRemoved, KnowledgeBaseIncomplete):
            result = UnitResult(
                "blocked", error="Knowledge base is unavailable; reopen or diagnose it", halt=True
            )
        except (RecoveryRequired, DirtyRollbackError):
            result = UnitResult("blocked", error="Knowledge base needs repair", halt=True)
        except ConnectionRequired as exc:
            result = UnitResult(
                "blocked", error=str(exc), halt=True, unfinished=("connection_setup",)
            )
        except Exception as exc:
            # Provider exceptions may contain prompts, URLs, headers or keys.
            # Public summaries carry a category, never an unchecked repr.
            result = UnitResult("failed", error=f"Operation failed ({type(exc).__name__})")
        from dataclasses import replace

        from ultrakb.application.connection_failures import shared_model_failure

        failure = shared_model_failure(getattr(context.executor, "model_errors", ()))
        if failure:
            result = replace(result, halt=True, error=failure.message)
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
