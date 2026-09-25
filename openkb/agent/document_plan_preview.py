"""Readable private preview of a third-step plan, derived without another model call."""

from __future__ import annotations

import json
import math
from typing import Any

from openkb.agent.document_plan import DocumentPlan, to_dict
from openkb.execution_measurement import record_document_totals
from openkb.locks import atomic_write_text
from openkb.sources import valid_id


def planning_execution_metrics(value: Any) -> dict[str, int | float]:
    """Normalize optional persisted counters before arithmetic or rendering."""
    row = value if isinstance(value, dict) else {}
    requests = row.get("planning_requests")
    elapsed = row.get("elapsed_seconds")
    return {
        "planning_requests": requests if type(requests) is int and requests >= 0 else 0,
        "elapsed_seconds": (
            float(elapsed)
            if isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
            and math.isfinite(elapsed)
            and elapsed >= 0
            else 0.0
        ),
    }


def render_plan_preview(plan: DocumentPlan) -> str:
    """Render the accepted overview and page routes without publishing wiki content."""
    execution = planning_execution_metrics(plan.metadata.get("planning_execution"))
    lines = [
        "# 文档规划预览",
        "",
        f"来源：`{plan.metadata.get('source_id', '')}`",
        f"解析：`{plan.metadata.get('parse_id', '')}`",
        f"规划状态：{plan.overview.status}",
        "",
        "## 本轮处理",
        "",
        f"规划请求：{execution.get('planning_requests', 0)} 次；"
        f"累计耗时：{execution.get('elapsed_seconds', 0.0):.1f} 秒",
        "",
        "## 概要",
        "",
        plan.overview.text or "（未形成概览）",
        "",
    ]
    if plan.overview.limitations:
        lines.extend(["### 限制", "", *[f"- {item}" for item in plan.overview.limitations], ""])
    coverage = plan.metadata.get("planning_coverage")
    if isinstance(coverage, dict):
        if coverage.get("protocol") == "document-planning-coverage-v2":

            def render_ratio(name: str) -> str:
                value = coverage.get(name)
                return "不可计算" if value is None else f"{value:.1%}"

            lines.extend(
                [
                    "## 规划范围统计",
                    "",
                f"明确定位主体：{render_ratio('precise_ratio')}（{coverage['precise_chars']} 字）",
                f"较宽取证范围：{render_ratio('fallback_ratio')}"
                f"（{coverage['fallback_chars']} 字）",
                f"未纳入主体：{render_ratio('unrouted_ratio')}（{coverage['unrouted_chars']} 字）",
                    "这些比例仅说明已解析原文的范围，不证明语义完整。",
                    "正文生成、审核、发布：尚未执行",
                    "",
                ]
            )
        else:
            ratio = coverage.get("effective_ratio")
            rendered = "不可计算" if ratio is None else f"{ratio:.1%}"
            lines.extend(
                [
                    "## 规划覆盖",
                    "",
                    f"可读文本有效规划覆盖率：{rendered}",
                    f"可执行页面：{coverage.get('executable_page_chars', 0)} 字；"
                    f"仅保留原文：{coverage.get('source_only_chars', 0)} 字；"
                    f"遗漏：{coverage.get('missing_chars', 0)} 字",
                    f"不可执行页面：{coverage.get('blocked_pages', 0)}；"
                    f"规划遗漏：{coverage.get('planning_omissions', 0)}；"
                    f"解析缺口：{coverage.get('parser_gaps', 0)}",
                    "正文生成、审核、发布：尚未执行",
                    "",
                ]
            )
    checks = [
        check
        for row in plan.metadata.get("accepted_window_receipts", [])
        if isinstance(row, dict)
        for check in [row.get("reference_check")]
        if isinstance(check, dict)
    ]
    if checks:
        lines.extend(["## 引用核对", ""])
        for index, check in enumerate(checks, 1):
            lines.append(
                f"- 窗口 {index}：{check['status']}；检测候选 {check['candidate_count']} 个"
            )
        lines.append("")
    lines.extend(["## 页面计划", ""])
    for page in plan.pages:
        lines.extend(
            [
                f"### {page.title}",
                "",
                f"状态：{page.state} · 目标：`{page.name}`",
                f"用途：{page.purpose}",
                f"主体原文范围：`{json.dumps(page.subject_ranges, ensure_ascii=False)}`",
                "",
            ]
        )
        for context in page.necessary_context:
            lines.append(
                f"- 必要上下文（{context['relation']}）："
                f"`{json.dumps(context['ranges'], ensure_ascii=False)}`"
            )
        if page.necessary_context:
            lines.append("")
        if page.context_ranges:
            lines.append(f"- 必要上下文：`{json.dumps(page.context_ranges, ensure_ascii=False)}`")
            lines.append("")
        for note in page.planning_notes:
            lines.append(f"- 规划提示（待原文核对）：{note}")
        if page.planning_notes:
            lines.append("")
        if page.scope_resolution == "target_fallback":
            lines.append("- 定位：使用当前目标的较宽原文范围，待进一步核对。")
            lines.append("")
        for limitation in page.limitations:
            lines.append(f"- 适用限制：{limitation.reason}（原文：{limitation.source_quote}）")
        if page.limitations:
            lines.append("")
    if plan.external_references:
        lines.extend(["## 外部参考（未关联）", ""])
        for reference in plan.external_references:
            lines.append(
                f"- {reference.target_document}"
                + (f" · {reference.target_section}" if reference.target_section else "")
                + f"：{reference.raw_quote}；受影响页面：{', '.join(reference.affected_pages)}"
            )
        lines.append("")
    if plan.unresolved:
        lines.extend(["## 未决问题", ""])
        for item in plan.unresolved:
            lines.append(
                f"- {item.missing_target}（{item.problem_type}，{item.status}）："
                f"{item.reason}；受影响页面：{', '.join(item.affected_pages)}"
            )
        lines.append("")
    if plan.planning_omissions:
        lines.extend(["## 本轮处理遗漏", ""])
        receipts = plan.metadata.get("accepted_window_receipts", [])
        receipts = receipts if isinstance(receipts, list) else []
        reference_attempts = {
            receipt["window"]: receipt["reference_check"].get("attempts", 0)
            for receipt in receipts
            if isinstance(receipt, dict)
            and isinstance(receipt.get("window"), str)
            and isinstance(receipt.get("reference_check"), dict)
        }
        for omission in plan.planning_omissions:
            reference_text = (
                f"；参考核对尝试 {reference_attempts[omission.target_id]} 次"
                if reference_attempts.get(omission.target_id)
                else ""
            )
            lines.append(
                f"- {omission.key}：{omission.reason}；部件 {omission.component}；"
                f"规划尝试 {omission.attempts} 次{reference_text}；受影响页面 "
                f"{', '.join(omission.affected_pages) or '无已知页面'}；"
                f"原文范围 `{json.dumps(omission.ranges, ensure_ascii=False)}`"
            )
        lines.extend(["", "补处理：对该来源执行“继续”，只重试仍开放的遗漏。"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_final_plan(checkpoints: Any, recovery_key: str, plan: DocumentPlan) -> None:
    """Persist the validated plan and its private Markdown view at one stable key."""
    name = f"{valid_id(recovery_key)}.md"
    directory = checkpoints.store.owned_path(checkpoints.root / "plan-preview")
    directory.mkdir(parents=True, exist_ok=True)
    path = checkpoints.store.owned_path(directory / name)
    plan.metadata["plan_preview"] = str(path)
    atomic_write_text(path, render_plan_preview(plan))
    checkpoints.save_recovery(recovery_key, "plan", to_dict(plan))
    record_document_totals(planned_pages=len(plan.pages))


def save_empty_planning_report(
    checkpoints: Any,
    recovery_key: str,
    metadata: dict[str, Any],
    omissions: list[Any],
    *,
    parsed: Any,
    execution: dict[str, Any],
) -> str:
    """Persist an inspectable zero-result outcome without inventing a formal plan."""
    name = f"{valid_id(recovery_key)}.json"
    directory = checkpoints.store.owned_path(checkpoints.root / "plan-report")
    directory.mkdir(parents=True, exist_ok=True)
    path = checkpoints.store.owned_path(directory / name)
    payload = {
        "protocol": "document-planning-report-v1",
        "source_id": metadata["source_id"],
        "version_id": metadata["version_id"],
        "parse_id": metadata["parse_id"],
        "outcome": "empty",
        "overview": None,
        "planning_omissions": [item.to_dict() for item in omissions],
        "planning_coverage": None,
        "planning_execution": planning_execution_metrics(execution),
        "retry_entry": "continue_source (only active omissions)",
    }
    from openkb.planning_coverage import planning_coverage

    payload["planning_coverage"] = planning_coverage(None, parsed, omission_count=len(omissions))
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    checkpoints.save_recovery(recovery_key, "plan_report", payload)
    return str(path)
