"""Admit child imports only from the current run's confirmed document receipts."""

from pathlib import Path

from openkb.runtime.requests import ImportAttachment
from openkb.runtime.task_submission import enqueue_task
from openkb.sources import content_id


def enqueue_attachments(manager, parent, result, identity):
    document = result.document
    if (
        document is None
        or not document.attachments
        or result.status in {"stopped", "blocked"}
        or result.halt
        or parent.view.stop_requested
    ):
        return True
    related = list(parent.view.child_task_ids)
    try:
        for child in document.attachments:
            binding = content_id(
                [
                    "attachment-import-v2",
                    identity.kb_dir,
                    identity.generation,
                    document.input_version,
                    child.version_id,
                ]
            )
            task_id = binding[:32]
            if task_id not in related and len(related) >= 10000:
                raise ValueError("Attachment task relationship limit exceeded")
            previous = manager._tasks.get(task_id)
            if previous is not None:
                if previous.input_binding != binding or previous.view.kb_dir != identity.kb_dir:
                    raise ValueError("Attachment task identity conflicts with existing history")
            else:
                enqueue_task(
                    manager,
                    Path(identity.kb_dir),
                    [
                        ImportAttachment(
                            child.source_id,
                            child.version_id,
                            document.input_version,
                            child.part,
                            child.name,
                        )
                    ],
                    task_id=task_id,
                    generation=identity.generation,
                    input_binding=binding,
                    parent_task_id=parent.view.id,
                    source_name=child.name,
                    snapshot=parent.snapshot,
                )
            if task_id not in related:
                related.append(task_id)
        manager._update(parent, child_task_ids=tuple(related))
        return True
    except (OSError, ValueError):
        # Keep every accepted child and make incomplete admission observable.
        # Reading task history must never silently replay the missing requests.
        manager._update(
            parent,
            child_task_ids=tuple(related),
            error=(
                "Attachment import tasks could not all be saved; "
                "continue the parent to retry admission"
            ),
            stop_requested=True,
        )
        return False


def task_family(manager, task_id):
    """Snapshot related work so CLI callers can await independently queued children."""
    pending = [task_id]
    seen = set()
    views = []
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            view = manager.get(current)
        except KeyError:
            continue  # Explicitly cleared historical summaries are not runnable work.
        views.append(view)
        pending.extend(view.child_task_ids)
    return tuple(views)
