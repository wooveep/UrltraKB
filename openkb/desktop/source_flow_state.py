"""Present recorded source progress without inferring publication from cached files."""

from dataclasses import dataclass

STAGES = (
    ("intake", "原文接入", "保存原始文件，保留独立的资料版本。"),
    ("parsing", "内容解析", "读取正文、图片和文档附件；缺失内容单独标记。"),
    ("facts", "事实提取", "从原文提取事实，并保存可回读的引文。"),
    ("planning", "主题规划", "整理主题与知识页面的对应关系。"),
    ("generation", "生成与校验", "生成页面并对照原文校验；草稿不代表已入库。"),
    ("publication", "知识入库", "提交已验证的知识变更，供浏览和问答使用。"),
)
PHASES = {
    "preparing": "intake",
    "source_intake": "intake",
    "parsing": "parsing",
    "docx": "parsing",
    "pdf": "parsing",
    "text": "parsing",
    "image_ocr": "parsing",
    "cloud_ocr": "parsing",
    "parse_cache": "parsing",
    "facts": "facts",
    "planning": "planning",
    "generation": "generation",
    "verification": "generation",
    "generated": "generation",
    "committing": "publication",
    "committed": "publication",
    "navigation": "publication",
}
STATE_LABELS = {
    "completed": "已完成",
    "pending": "待处理",
    "running": "进行中",
    "paused": "已暂停",
    "failed": "异常",
    "review": "待处理",
    "unknown": "待确认",
    "queued": "等待执行",
    "stopping": "正在停止",
}


@dataclass(frozen=True)
class FlowStep:
    key: str
    title: str
    state: str
    current: bool = False
    progress: str = ""


def source_snapshot(document):
    """Seed the inspector from the already-read inventory while disk reads wait."""
    return {
        "source": {
            "source_id": document["source_id"],
            "id": document["source_version"],
            "name": document["name"],
            "suffix": "." + document.get("type", ""),
            "origin": document.get("source_origin"),
        },
        "original": document.get("original"),
        "result": {
            key: document.get(key)
            for key in (
                "source_intake",
                "knowledge_compilation",
                "stage",
                "reason",
                "parse_id",
                "omissions",
            )
        },
        "cumulative_usage": document.get("cumulative_usage", {}),
    }


def flow_steps(saved, activity=None):
    """Activity describes only the selected source's current processing item."""
    result = (saved or {}).get("result") or {}
    phase = result.get("stage")
    state = result.get("knowledge_compilation", "not_started")
    reason = result.get("reason")
    progress = ()
    if activity:
        phase, state, progress = activity.stage, activity.state, activity.progress
        reason = None  # An earlier failure must not label a fresh running request.
    current = PHASES.get(phase)
    if state == "stopping" and current is None:
        current = next((PHASES[p.phase] for p in progress if p.phase in PHASES), None)
    if phase == "parsed":
        current, state = "facts", "pending"
    if state == "not_started" and current in {None, "intake"}:
        current = "parsing"
    if phase in {"waiting", "waiting-input", "queued", "starting"}:
        current = None
    if state == "completed" and not activity:
        current = "publication"
    committed = (state == "completed" and not activity) or phase in {"committed", "navigation"}
    indices = {key: i for i, (key, _, _) in enumerate(STAGES)}
    position = indices.get(current, -1)
    rows = []
    for index, (key, title, _) in enumerate(STAGES):
        status = "pending"
        if saved and key == "intake":
            status = "completed"
        if result.get("parse_id") and key == "parsing" and current != "parsing":
            status = "completed"
        if current and index < position:
            status = "completed"
        if committed:
            status = "completed"
        elif key == current:
            status = {
                "running": "running",
                "waiting": "queued",
                "queued": "queued",
                "stopping": "stopping",
                "failed": "failed",
                "stopped": "paused",
                "unfinished": "paused",
                "interrupted": "paused",
                "blocked": "review",
            }.get(state, "pending")
            if reason in {"source_quality_needs_review", "needs_acceptance", "input_conflict"}:
                status = "review"
        elif (
            current is None
            and key in {"facts", "planning", "generation"}
            and state not in {"not_started", "queued", "waiting"}
        ):
            status = "unknown"
        counter = next((p for p in progress if PHASES.get(p.phase) == key and p.total), None)
        omitted = [row for row in (result.get("omissions") or ()) if row["stage"] == key]
        if committed and omitted:
            status = "review"
        rows.append(
            FlowStep(
                key,
                title,
                status,
                key == current,
                f"{counter.percent}% · {counter.completed}/{counter.total}"
                if counter
                else (f"已排除 {sum(len(row['items']) for row in omitted)} 项" if omitted else ""),
            )
        )
    return tuple(rows)
