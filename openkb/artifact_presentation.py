"""Shared meaning of artifact checks across CLI, API and desktop."""

from openkb.artifact_quality import unknown_quality


def quality_summary(record: dict | None) -> str:
    value = record or unknown_quality()
    checks = value.get("checks", {})
    labels = {"passed": "通过", "failed": "有问题", "unknown": "未知", "not_checked": "未检查"}
    status = {"checked": "已检查", "issues": "有问题", "stale": "已失效", "unknown": "未知"}
    return (
        f"质量记录：{status.get(value['status'], '未知')}；"
        f"格式：{labels.get(checks.get('format'), '未知')}；"
        f"引用：{labels.get(checks.get('citations'), '未知')}；"
        "语义支持与必要事实覆盖：未检查。"
    )


def generation_facts(result) -> dict:
    return {
        "execution": result.status,
        "quality": list(result.quality),
        "unfinished": list(result.unfinished),
        "resources": list(result.resources),
        "artifact_quality": result.artifact_quality or unknown_quality(),
    }


def generation_summary(result) -> str:
    lines = [f"执行：{result.status}", quality_summary(result.artifact_quality)]
    lines.extend(f"质量提示：{issue}" for issue in result.quality)
    lines.extend(f"未完成：{stage}" for stage in result.unfinished)
    lines.extend(f"保存文件：{path}" for path in result.resources)
    return "\n".join(lines)
