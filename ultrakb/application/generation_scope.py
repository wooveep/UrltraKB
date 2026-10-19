"""Bind each generation request to its original publication before any paid work."""

from pydantic import BaseModel, ConfigDict, Field

from ultrakb.compile_checkpoints import digest
from ultrakb.mutation import RecoveryRequired
from ultrakb.source_records import RecordId, ViewId
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.generation_store import generation_request

from .views import storage_scope


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    publication_id: RecordId
    view_id: ViewId
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")


def generation_unit(scope, context):
    if context.resume_storage_execution_id is not None:
        return ExecutionStore(scope.kb_dir, scope.knowledge.identity).execution_unit(
            context.resume_storage_execution_id
        )
    return context.usage_unit_id


def generation_request_key(scope, options, context):
    return generation_request(scope, options, unit_id=generation_unit(scope, context))


def previous_generation_execution(scope, context):
    if context.storage_execution is not None:
        return context.storage_execution
    store = ExecutionStore(scope.kb_dir, scope.knowledge.identity)
    if context.resume_storage_execution_id is not None:
        return store.execution(context.resume_storage_execution_id)
    if context.usage_task_id is not None:
        return store.application_unit_execution(context.usage_task_id, context.usage_unit_id)
    return None


def _identity(scope, context):
    return {
        "operation": "generation-inputs-v2",
        "view_id": scope.view_id,
        "unit_id": generation_unit(scope, context),
    }


def restore_generation_scope(scope, options, context, *, explicit):
    previous = previous_generation_execution(scope, context)
    if previous is None:
        return scope
    identity = _identity(scope, context)
    work = CompileCheckpointStore(scope.kb_dir, scope.knowledge.identity).find_work(
        previous.model_budget_id,
        object_id=identity,
        conditions={"generation-inputs": 1},
        execution_id=previous.execution_id,
    )
    retained = work.load("publication", digest(identity)) if work else None
    if retained is None:
        store = ExecutionStore(scope.kb_dir, scope.knowledge.identity)
        same_unit = store.execution_unit(previous.execution_id) == generation_unit(scope, context)
        if context.resume_storage_execution_id is not None or (
            same_unit and store.budget(previous.model_budget_id).used
        ):
            raise RecoveryRequired("The original generation input binding is unavailable")
        return scope
    bound = _Binding.model_validate(retained)
    restored = storage_scope(
        scope.kb_dir,
        scope.knowledge.identity,
        publication_id=bound.publication_id,
        view_id=bound.view_id,
    )
    if (
        bound.request_key != generation_request_key(restored, options, context)
        or bound.view_id != scope.view_id
        or (explicit and scope.publication_id != bound.publication_id)
    ):
        raise RecoveryRequired("Generation requires its original fixed publication and options")
    return restored


def retain_generation_scope(scope, options, context):
    identity = _identity(scope, context)
    work = CompileCheckpointStore(scope.kb_dir, scope.knowledge.identity).for_work(
        context.storage_execution.model_budget_id,
        object_id=identity,
        conditions={"generation-inputs": 1},
        execution_id=context.storage_execution.execution_id,
    )
    bound = _Binding(
        publication_id=scope.publication_id,
        view_id=scope.view_id,
        request_key=generation_request_key(scope, options, context),
    ).model_dump(mode="json")
    retained = work.load("publication", digest(identity))
    if retained is not None and retained != bound:
        raise RecoveryRequired("Generation cannot replace its retained input binding")
    if retained is None:
        work.save("publication", digest(identity), bound)
