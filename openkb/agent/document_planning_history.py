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
        if candidate.get("protocol") == "document-planning-markdown-v1":
            # The original fragment-era shape is checked as such; it never
            # becomes a current cumulative-overview state.
            candidate = {**candidate, "protocol": "document-planning-acceptance-v2"}
        if not _valid_state(candidate, windows) or not candidate["responses"]:
            continue
        current = (
            candidate.get("protocol")
            in {"document-planning-acceptance-v3", "document-planning-acceptance-v4"}
            and "overview_snapshot" in candidate
        )
        tasks = {
            key: deepcopy(task)
            for key, task in candidate["tasks"].items()
            if current and key.endswith(":overview")
        }
        valid_tasks = {
            window_receipt_id(window) + ":" + part
            for window in candidate["windows"]
            for part in ("overview", "pages")
        }
        for response in candidate["responses"]:
            task_key = response.get("task")
            if isinstance(task_key, str) and task_key.endswith(":overview"):
                continue
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
                    **({"binding": response["binding"]} if response.get("binding") else {}),
                    **(
                        {"projection": response["projection"]} if response.get("projection") else {}
                    ),
                }
            )
        retained = list(candidate["retained_fragments"])
        retained.extend(
            {
                "start": window["target_start"],
                "text": candidate["fragments"][window_receipt_id(window)],
            }
            for window in candidate["windows"]
            if window_receipt_id(window) in candidate["fragments"]
        )
        if tasks or retained:
            return (
                candidate["windows"],
                tasks,
                key,
                retained if current else [],
                candidate.get("overview_snapshot") if current else None,
                candidate.get("overview_history", []) if current else [],
                candidate.get("legacy_overview_fragments", []) if current else retained,
            )
    return None
