"""Generate a fixed-evidence graph as an independently accepted artifact."""

import logging
from contextlib import contextmanager
from dataclasses import replace

from ultrakb.lifecycle import read_lifecycle
from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.visualize import render_html

from .artifacts import GraphResult, read_graph
from .execution import ExecutionContext
from .storage_artifacts import commit_artifact

logger = logging.getLogger(__name__)


def _retained(result):
    return result is not None and result.artifact_revision is not None


def _pending_confirmation(result, reason):
    return replace(
        result,
        confirmation_pending=True,
        reason_code="artifact_conflict"
        if result.artifact_revision.status == "conflict"
        else reason,
    )


@contextmanager
def _stage(store, execution_id, code):
    stage = store.start_stage(execution_id, code, work_key="output/visualize/graph.html")
    try:
        yield
    except BaseException as error:
        try:
            store.finish_stage(
                stage,
                state="cancelled" if isinstance(error, LockCancelled) else "failed",
                error_code=type(error).__name__,
            )
        except Exception:
            logger.warning("Graph stage confirmation failed", exc_info=True)
        raise
    else:
        if not store.finish_stage(stage, state="completed"):
            raise RecoveryRequired("Graph stage completion needs confirmation")


def generate_stored_graph(root, scope, context):
    identity = scope.knowledge.identity
    context = context or ExecutionContext()
    if context.storage_identity is not None and context.storage_identity != identity:
        raise ValueError("Graph execution belongs to another storage generation")
    result, failure, state = None, None, "completed"
    try:
        with read_lifecycle(
            root, database_identity=identity, cancelled=context.cancelled, on_wait=context.waiting
        ):
            context.check_stop()
            executions = ExecutionStore(root, identity)
            execution = executions.begin(
                "generate_graph", fingerprint="graph-v1:" + scope.publication_id, max_calls=0
            )
            try:
                artifacts = ArtifactStore(root, identity)
                epoch = artifacts.epoch()
                with _stage(executions, execution.execution_id, "graph.collect.v1"):
                    context.on_event({"stage": "generating_graph"})
                    graph = read_graph(root, scope=scope)
                    context.check_stop()
                result = GraphResult(graph, execution_id=execution.execution_id)
                if graph["nodes"]:
                    with _stage(executions, execution.execution_id, "graph.render.v1"):
                        payload = render_html(graph).encode("utf-8")
                        context.check_stop()
                    with _stage(executions, execution.execution_id, "artifact.accept.v1"):
                        revision = commit_artifact(
                            artifacts,
                            "output/visualize/graph.html",
                            payload,
                            expected_epoch=epoch,
                            execution_id=execution.execution_id,
                            evidence_publication_id=scope.publication_id,
                            check_cancelled=context.check_stop,
                        )
                        # Latch master truth before projection or stage receipt I/O.
                        result = replace(
                            result,
                            artifact_revision=revision,
                            reason_code="artifact_conflict"
                            if revision.status == "conflict"
                            else "artifact_sync_pending",
                        )
                    if revision.status == "saved":
                        with _stage(executions, execution.execution_id, "artifact.project.v1"):
                            projection = artifacts.sync(
                                revision.path,
                                revision_id=revision.revision_id,
                                check_cancelled=context.check_stop,
                            )
                            result = replace(
                                result,
                                projection=projection,
                                path=root / revision.path
                                if projection.status == "applied"
                                else None,
                                reason_code=None
                                if projection.status == "applied"
                                else "artifact_sync_" + projection.status,
                            )
                    else:
                        state = "failed"
            except BaseException as error:
                failure = error
                state = "cancelled" if isinstance(error, LockCancelled) else "failed"
                if not isinstance(error, Exception) or not _retained(result):
                    raise
                result = _pending_confirmation(result, "graph_confirmation_pending")
            finally:
                try:
                    if executions.status(execution.execution_id) == "stopping":
                        state = "cancelled"
                    executions.finish(execution.execution_id, state=state, work_settled=True)
                except Exception:
                    if _retained(result):
                        result = _pending_confirmation(result, "execution_confirmation_pending")
                    elif failure is None:
                        raise
                    else:
                        logger.warning("Graph execution confirmation failed", exc_info=True)
        return result
    except Exception:
        if _retained(result):
            return _pending_confirmation(result, "execution_confirmation_pending")
        raise
