"""Replay source-bound raw planning responses under the current acceptance rules."""

from copy import deepcopy

from openkb.agent.document_window_receipts import window_receipt_id


def previous_responses(checkpoints, windows):
    """Only explicit Continue calls this; no old page or publication state is adopted."""
    from openkb.agent.document_markdown_planner import _valid_state

    paths = sorted(
        (checkpoints.root / "recovery").glob("*-markdown_plan.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in paths:
        key = path.name.removesuffix("-markdown_plan.json")
        previous = checkpoints.load_recovery(key, "markdown_plan")
        if not isinstance(previous, dict):
            continue
        candidate = deepcopy(previous)
        candidate["protocol"] = "document-planning-acceptance-v2"
        if not _valid_state(candidate, windows) or not candidate["responses"]:
            continue
        tasks = {}
        valid_tasks = {
            window_receipt_id(window) + ":" + part
            for window in candidate["windows"]
            for part in ("overview", "pages")
        }
        for response in candidate["responses"]:
            task_key = response.get("task")
            if (
                isinstance(task_key, str)
                and task_key.endswith(":pages")
                and task_key not in valid_tasks
            ):
                # Retired parent suggestions still belong to this full source.
                task_key = window_receipt_id(candidate["windows"][0]) + ":pages"
            if task_key not in valid_tasks or not isinstance(response.get("content"), str):
                continue
            task = tasks.setdefault(
                task_key,
                {"status": "pending", "attempts": 0, "raw": None, "reason": None, "replay": []},
            )
            task["replay"].append(
                {
                    "content": response["content"],
                    "finish_reason": response.get("finish_reason", "stop"),
                }
            )
        from openkb.agent.document_planning_response import accept_overview

        retained = []
        for fragment in candidate["retained_fragments"]:
            accepted = accept_overview(fragment["text"])
            if accepted.text and not accepted.reason:
                retained.append({"start": fragment["start"], "text": accepted.text})
        if retained:
            for task_key in valid_tasks:
                old_task = candidate["tasks"].get(task_key, {})
                if (
                    task_key.endswith(":overview")
                    and task_key not in tasks
                    and old_task.get("status") == "accepted"
                    and old_task.get("attempts") == 0
                ):
                    tasks[task_key] = {
                        "status": "accepted",
                        "attempts": 0,
                        "raw": None,
                        "reason": None,
                    }
        if tasks:
            return candidate["windows"], tasks, key, retained
    return None
