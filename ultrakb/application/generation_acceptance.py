"""Accept exact generator files before projecting them to the output directory."""

from contextlib import nullcontext
from dataclasses import replace

from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired
from ultrakb.skill.validator import ValidationResult
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.generation_store import GenerationStore

from .execution import current_execution
from .generators import GenerationResult
from .task_stages import task_stage


def _result(root, plan, accepted, *, status, unfinished=(), error_type=None):
    details = plan.details
    archive_saved = details.archive_path is not None and (
        details.output_dir != details.archive_path
        or any(
            item.operation == "write" and item.path.startswith(details.archive_path + "/")
            for item in accepted
        )
    )
    return GenerationResult(
        status,
        output_dir=root / details.output_dir,
        artifact_path=root / details.artifact_path,
        archive_path=root / details.archive_path if archive_saved else None,
        validation=ValidationResult(
            errors=list(details.validation_errors), warnings=list(details.validation_warnings)
        )
        if details.validation_known
        else None,
        generation_id=plan.plan_id,
        artifact_epoch=max((item.epoch for item in accepted), default=plan.expected_epoch),
        resources=tuple(str(root / item.path) for item in accepted if item.operation == "write")
        + ((str(root / details.archive_path),) if archive_saved else ()),
        changes=tuple(
            f"{'removed' if item.operation == 'delete' else 'saved'}: {item.path}"
            for item in accepted
        ),
        quality=details.quality
        + tuple(
            issue
            for issue, present in (
                ("validation_errors", details.validation_errors),
                ("validation_warnings", details.validation_warnings),
            )
            if present
        ),
        projection_pending=bool(accepted),
        unfinished=unfinished,
        error_type=error_type,
    )


def accept_generation(scope, plan, context):
    root, identity = scope.kb_dir, scope.knowledge.identity
    observed = current_execution() is context
    result = None
    accepted = ()
    try:
        with (
            task_stage(context, "generator.accept.v1", root=root) if observed else nullcontext({})
        ) as progress:
            outcome = GenerationStore(root, identity).accept(
                plan,
                context.storage_execution.execution_id if observed else None,
                check_cancelled=context.check_stop,
            )
            accepted, conflict, error = outcome.files, outcome.conflict, outcome.error
            if error is not None and not accepted:
                raise error
            result = _result(
                root,
                plan,
                accepted,
                status="conflict"
                if conflict
                else "blocked"
                if isinstance(error, RecoveryRequired)
                else "failed"
                if error is not None
                else "completed"
                if plan.details.generation_complete
                else "failed",
                unfinished=("review_artifact",)
                if conflict
                else ("generation",)
                if error is not None
                else plan.details.unfinished,
                error_type=type(error).__name__ if error else plan.details.error_type,
            )
            context._storage_result = "completed" if result.status == "completed" else "failed"
            if error is not None or result.status != "completed":
                progress["status"] = "cancelled" if isinstance(error, LockCancelled) else "failed"
                progress["reason_code"] = result.error_type or result.status
    except Exception:
        if result is None:
            raise
        result = replace(result, unfinished=(*result.unfinished, "confirm acceptance observation"))
    pending = False
    try:
        with task_stage(context, "generator.sync.v1", root=root) if observed else nullcontext():
            if plan.details.archive_path and plan.details.output_dir != plan.details.archive_path:
                archive = GenerationStore(root, identity).archive(plan)
                if archive is None:
                    raise RecoveryRequired("The independently accepted archive is unavailable")
                main_status = context._storage_result
                try:
                    previous = accept_generation(scope, archive, context)
                finally:
                    context._storage_result = main_status
                pending |= previous.projection_pending or previous.status != "completed"
            artifacts = ArtifactStore(root, identity)
            for item in accepted:
                projection = artifacts.sync(
                    item.path, revision_id=item.revision_id, check_cancelled=context.check_stop
                )
                pending |= projection.status not in {"applied", "superseded"}
    except Exception:
        pending = True
    return replace(
        result,
        projection_pending=pending,
        unfinished=(*result.unfinished, *(("synchronize artifacts",) if pending else ())),
    )
