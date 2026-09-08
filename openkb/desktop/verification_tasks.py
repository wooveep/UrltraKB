"""Observe real submitted work without depending on transient Qt polling state."""

from collections.abc import Callable

from openkb.runtime.records import TERMINAL, TaskView
from openkb.runtime.tasks import TaskManager


class SubmittedTasks:
    def __init__(self, manager: TaskManager, wait_until: Callable) -> None:
        self.manager = manager
        self.wait_until = wait_until
        self.known = {task.id for task in manager.tasks()}

    def finish(self, displayed: Callable[[], bool]) -> TaskView:
        # A wait may pump Qt once more before returning. Dialogs can clear
        # their live task in that turn, but the scheduler retains its record.
        self.wait_until(lambda: any(task.id not in self.known for task in self.manager.tasks()))
        submitted = {task.id for task in self.manager.tasks()} - self.known
        assert len(submitted) == 1, submitted
        task_id = submitted.pop()
        self.known.add(task_id)
        self.wait_until(
            lambda: self.manager.get(task_id).state in TERMINAL
            and self.manager.get(task_id).processes_reaped
        )
        self.wait_until(displayed)
        return self.manager.get(task_id)


def assert_result_text(task: TaskView, text: str, *, resources: bool = False) -> None:
    """The visible result must include this task's changes and incomplete work."""
    expected = [task.error] if task.error else []
    for result in task.results:
        expected.extend(result.changes)
        expected.extend(result.quality)
        expected.extend(result.unfinished)
        if result.error:
            expected.append(result.error)
        if resources:
            expected.extend(result.resources)
    assert all(fragment in text for fragment in expected), (expected, text)


def finished_document(tasks: SubmittedTasks, dialog) -> TaskView:
    task = tasks.finish(lambda: dialog._task is None)
    expected = (
        "任务已执行，含质量提示或未完成阶段，请查看结果。"
        if any(result.quality or result.unfinished for result in task.results)
        else "任务完成。"
        if task.state == "completed"
        else "任务未全部完成。请查看逐项结果；确认最新资料后可手动重试。"
    )
    assert dialog.status.text() == expected, dialog.status.text()
    assert_result_text(task, dialog.details.toPlainText(), resources=True)
    return task
