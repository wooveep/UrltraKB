"""Persist new work before it can enter the scheduler's executable queue."""

from openkb.runtime.records import TaskView, UnitIdentity


def enqueue_task(
    manager,
    root,
    units,
    *,
    task_id,
    generation,
    retry_of=None,
    input_binding=None,
    parent_task_id=None,
    source_name=None,
    snapshot=None,
):
    from openkb.runtime.tasks import _Task

    view = TaskView(
        task_id,
        str(root),
        type(units[0]).__name__,
        "queued",
        "queued",
        len(units),
        (),
        False,
        True,
        retry_of=retry_of,
        parent_task_id=parent_task_id,
        source_name=source_name,
    )
    task = _Task(
        view,
        tuple(units),
        tuple(
            UnitIdentity.create(task_id, i, str(root), request, generation=generation)
            for i, request in enumerate(units)
        ),
        snapshot=snapshot,
        input_binding=input_binding,
    )
    manager._persist(task)
    manager._tasks[task_id] = task
    manager._pending.append(task_id)
    manager._condition.notify_all()
    return task_id
