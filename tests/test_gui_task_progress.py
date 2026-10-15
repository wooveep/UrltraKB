"""Current work stays distinguishable from retained failures in the same batch."""

from ultrakb.desktop.task_status import task_progress
from ultrakb.runtime.records import TaskView, UnitResult


def test_prior_failure_does_not_replace_the_current_compile_stage():
    task = TaskView(
        "a" * 32,
        "/fixture/kb",
        "ImportFile",
        "running",
        "compiling",
        2,
        (UnitResult("failed", error="first-file failure"),),
        False,
        False,
        error="first-file failure",
        current_source="second.md",
    )
    assert task_progress(task) == "编译摘要、概念与实体"
