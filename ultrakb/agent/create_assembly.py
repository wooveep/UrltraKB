"""Present page-wide and local source scopes without rewriting accepted evidence."""

from collections import Counter
from pathlib import PurePosixPath


def _identity(window: dict) -> tuple[str, str, str]:
    return (
        window.get("path", ""),
        window.get("source_revision_id") or "",
        window.get("sha256") or "",
    )


def _title(path: str) -> str:
    name = PurePosixPath(path).name
    for suffix in (".content.json", ".json", ".md"):
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name or "来源"


def _numbers(values: set[int]) -> str:
    runs: list[list[int]] = []
    for number in sorted(values):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return "、".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)


def _spans(values: list) -> str:
    runs: list[list[int]] = []
    for start, end in sorted(values):
        if runs and start <= runs[-1][1]:
            runs[-1][1] = max(end, runs[-1][1])
        else:
            runs.append([start, end])
    return "、".join(f"[{a}, {b})" for a, b in runs)


def _source_scopes(windows: list[dict], names: dict) -> list[str]:
    if not windows:
        return ["来源位置未记录（仅保留已接受内容）"]
    grouped: dict[tuple, list[dict]] = {}
    for window in windows:
        grouped.setdefault(_identity(window), []).append(window)
    result = []
    for identity, selected in grouped.items():
        pages: dict[str, set[int]] = {}
        spans = []
        partial_text = structure_gap = False
        for window in selected:
            kind = "幻灯片" if window.get("unit_kind") == "slide" else "物理页"
            for span in window.get("page_spans", []):
                pages.setdefault(kind, set()).add(span["page"])
            if not window.get("page_spans"):
                spans.extend(window.get("source_spans", []))
            for unit in window.get("units", []):
                partial_text |= unit.get("text_coverage") not in {None, "complete"}
                structure_gap |= unit.get("structure_coverage") not in {
                    None,
                    "recorded",
                    "complete",
                }
        positions = [f"{kind} {_numbers(numbers)}" for kind, numbers in pages.items()]
        if spans:
            positions.append(f"字符区间 {_spans(spans)}")
        if not positions:
            positions.append("已接受的所选正文位置（未记录可显示的位置）")
        note = "（所选正文范围）"
        if partial_text:
            note += "；含局部正文片段或正文覆盖尚未确认"
        if structure_gap:
            note += "；结构完整性尚未确认"
        result.append(f"《{names[identity]}》：{'；'.join(positions)}{note}")
    return result


def assemble_contributions(values: list[dict], windows: list[list[dict]]) -> str:
    """Scope accepted bodies using actual reader positions, never model/planned ranges.

    Body bytes, including quotations and local limitations, remain untouched.
    Request prompts/signatures and checkpoint payloads therefore remain reusable.
    """
    if len(values) != len(windows) or not values:
        raise ValueError("Every accepted contribution requires its own source windows")
    if len(values) == 1:
        return values[0]["content"]
    all_windows = [w for group in windows for w in group]
    identities = dict.fromkeys(_identity(w) for w in all_windows)
    counts = Counter(identity[0] for identity in identities)
    names = {
        identity: _title(identity[0])
        + (
            f"（来源版本 {(identity[1] or identity[2] or '未记录')[:12]}）"
            if counts[identity[0]] > 1
            else ""
        )
        for identity in identities
    }
    scopes = _source_scopes(all_windows, names)
    parts = [
        "## 本页来源范围\n\n"
        "本页汇总以下来源位置的已接受内容；范围指所选正文，"
        "不代表整份资料或全部内容均已完整识别。\n\n"
        + "\n".join(f"- {scope}。" for scope in scopes)
        + "\n\n下文按来源分节。节内“本次读取”“窗口外”等范围说明仅适用于所在节，"
        "不表示整页缺少对应内容；原文中的真实限制和识别缺口继续保留。"
    ]
    for index, (value, selected) in enumerate(zip(values, windows, strict=True), 1):
        local = "；".join(_source_scopes(selected, names))
        parts.append(
            f"---\n\n**来源分节 {index} / {len(values)}**\n\n本节来源：{local}。"
            "本节范围说明仅指本节；整页范围见页首。\n\n" + value["content"].strip()
        )
    return "\n\n".join(parts)
