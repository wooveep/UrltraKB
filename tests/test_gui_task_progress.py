"""Current work stays distinguishable from retained failures in the same batch."""

import pytest

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


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("running", "正在执行"),
        ("waiting", "等待知识库可用"),
        ("stopping", "正在安全停止"),
        ("queued", "等待执行"),
    ],
)
def test_detail_distinguishes_current_unit_from_later_queued_units(state, expected):
    pytest.importorskip("PySide6")
    from ultrakb.desktop.task_details import task_text

    task = TaskView(
        "a" * 32,
        "/fixture/kb",
        "ImportFile",
        state,
        "compiling" if state == "running" else state,
        3,
        (UnitResult("completed"),),
        state == "stopping",
        False,
        current_source="second.docx",
    )
    text = task_text(task)
    assert f"第 2 项：{expected}" in text
    later = "尚未执行；已请求停止" if state == "stopping" else "等待执行"
    assert f"第 3 项：{later}" in text
    assert "资料处理进度：已处理 1 / 3 项" in text


@pytest.mark.parametrize("state", ["interrupted", "failed", "stopped"])
def test_terminal_unconfirmed_units_do_not_claim_waiting_or_running(state):
    pytest.importorskip("PySide6")
    from ultrakb.desktop.task_details import task_text

    task = TaskView("a" * 32, "/fixture/kb", "ImportFile", state, state, 2, (), False, True)
    text = task_text(task)
    assert "等待执行" not in text and "正在执行" not in text
    assert "第 2 项：未执行" in text
    expected = "结果未确认" if state == "interrupted" else "未执行"
    assert f"第 1 项：{expected}" in text


def test_budget_labels_are_counts_and_never_document_completion_ratios():
    from ultrakb.llm_usage import describe_model_usage

    text = "\n".join(describe_model_usage(model_usage_receipt()))
    assert "已发送 271 次" in text
    assert "上限 1000 次" in text
    assert "剩余 729 次" in text
    assert "不代表资料完成进度" in text
    assert "271 / 1000" not in text


def test_details_separate_embedded_worker_and_legacy_budget_snapshots():
    pytest.importorskip("PySide6")
    from ultrakb.desktop.task_details import task_text
    from ultrakb.ingest_result import IngestResult, describe_ingest

    receipt = model_usage_receipt()
    imported = IngestResult(
        "first.docx",
        "added",
        (),
        source_id="d" * 32,
        source_revision_id="e" * 32,
        model_usage=receipt,
    )
    result = UnitResult(
        "completed",
        changes=describe_ingest(imported)
        + ("模型任务累计发送：16 / 1000；任务：" + "a" * 32, "模型调用预算不足，请检查设置"),
        model_usage=receipt,
    )
    task = TaskView(
        "a" * 32,
        "/fixture/kb",
        "ImportFile",
        "running",
        "compiling",
        2,
        (result,),
        False,
        False,
        model_usage=receipt,
    )
    text = task_text(task, include_budget=False)
    assert "Source: " in text and "模型用量（本次）" in text
    assert "第 2 项：正在执行" in text
    assert "模型调用预算：已发送" not in text
    assert "模型调用预算不足，请检查设置" in text
    assert "模型任务累计发送" not in text
    assert "16 / 1000" not in text


def model_usage_receipt():
    """Explicit valid usage snapshot; no model requests are sent by GUI probes."""
    aggregate = {
        "input_total": 100,
        "output_total": 10,
        "total_known": 110,
        "cached_input": 0,
        "reasoning_output": 0,
        "requests": 1,
        "actual_sends": 1,
        "unsent_attempts": 0,
        "send_unknown": 0,
        "unknown_requests": 0,
        "in_flight_requests": 0,
        "unknown_fields": dict.fromkeys(
            ("input_total", "output_total", "cached_input", "reasoning_output"), 0
        ),
        "collection_complete": True,
        "usage_complete": True,
        "request_ids": ["b" * 32],
        "stages": {},
    }
    return {
        "current": aggregate,
        "cumulative": aggregate,
        "execution_ids": ["c" * 32],
        "source_ids": [],
        "ledger": "/fixture/kb/.ultrakb/usage",
        "history_status": "execution_only",
        "model_tasks": [{"task_id": "a" * 32, "actual_sends": 271, "limit": 1000}],
    }
