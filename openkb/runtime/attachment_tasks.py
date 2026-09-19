"""Read retained historical task families without scheduling attachment work."""


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
