"""Readable source status and evidence positions for the native review dialog."""

KINDS = {"heading": "标题", "paragraph": "正文", "table": "表格", "code": "代码", "image": "图像"}
REASONS = {
    "needs_acceptance": "已有页面包含人工修改，请在“知识变更”中检查差异。",
    "input_version_conflict": "资料或知识页面已变化，请刷新后继续处理。",
    "input_budget_exceeded": "请求超过模型上下文容量，请检查处理额度。",
    "section_coverage_incomplete": "模型未覆盖全部原文片段，本轮知识尚未发布。可继续处理。",
    "section_empty_without_reason": "部分片段没有事实或空结果说明，需要继续处理。",
    "fact_evidence_invalid": "提取事实的引文无法在对应原文中核对。",
    "topic_coverage_incomplete": "主题规划未包含全部事实主题，本轮知识尚未发布。",
    "topic_plan_invalid": "模型返回的主题规划不完整或格式不正确。",
    "topic_generation_incomplete": "模型未完整生成本主题的知识内容。",
    "knowledge_evidence_mismatch": "生成内容未通过原文证据复核，本轮知识尚未发布。",
    "evidence_verification_invalid": "原文复核结果不完整或已失效，本轮知识尚未发布。",
    "topic_title_conflict": "不同片段使用了不一致的主题标题，本轮知识尚未发布。",
    "topic_context_exceeds_request_budget": "单个主题及必要上下文超过请求额度，请检查模型容量。",
    "topic_evidence_exceeds_request_budget": "必要证据及上下文超过请求额度，请检查模型容量。",
    "generated_asset_evidence_invalid": "生成内容引用了无法核对的图像，本轮知识尚未发布。",
    "request_budget_exhausted": "本轮模型请求额度已用完。",
    "token_budget_exhausted": "本轮 token 额度已用完。",
    "time_budget_exhausted": "本轮处理达到时限。",
    "output_budget_exhausted": "模型输出达到上限，未完整结束。",
    "navigation_interrupted": "导航处理被中断，可单独重建。",
    "navigation_stopped": "导航处理已停止，可单独重建。",
    "quality_incomplete": "部分原文仍需检查，请打开“逐页检查”。",
    "blank_or_illustration": "需要确认空白或插图页",
    "ocr_blank_or_illustration": "识别后仍需确认空白或插图页",
    "image_content_requires_ocr": "图片内容需要识别或人工检查",
    "invisible_text_layer": "文字层不可见，需要重新识别",
    "unmapped_native_glyphs": "文字层含无法识别的字符",
    "native_table_structure_uncertain": "表格结构需要检查",
    "ocr_runtime_not_installed": "请先配置可选 OCR 运行环境",
    "ocr_model_package_missing": "请先配置离线模型包",
    "ocr_memory_budget_exhausted": "识别达到内存上限",
    "ocr_time_budget_exhausted": "识别达到时限",
    "ocr_recognition_budget_exhausted": "识别区域或输出额度已用完",
    "ocr_output_incomplete": "识别输出未完整结束，需要重新处理",
    "cloud_submission_unknown": "云提交结果不明，重新提交前需要明确确认",
}


def position_text(location):
    parts = []
    if location.get("page"):
        parts.append(f"原文第 {location['page']} 页")
    if location.get("headings"):
        parts.append(" › ".join(location["headings"]))
    for field, label in (("paragraph", "段落"), ("table", "表"), ("row", "行"), ("cell", "单元格")):
        if location.get(field):
            parts.append(f"{label} {location[field]}")
    if location.get("line"):
        label = "转换产物行" if location["kind"] == "converted" else "行"
        parts.append(f"{label} {location['line']}")
    return " · ".join(parts) or "原文片段"


def quality_text(rows):
    pending = [row for row in rows if row["status"] == "needs_review"]
    if not pending:
        return "解析完整性检查已通过。可选择片段回读原文。"
    return "需要检查的范围：\n" + "\n".join(
        f"第 {row['page']} 页：" + REASONS.get(row["reason"], row["reason"])
        if row.get("page")
        else REASONS.get(row["reason"], row["reason"])
        for row in pending
    )


def status_text(value):
    result, usage = value["result"] or {}, value["cumulative_usage"]
    rows = ["原文已保存，后续处理失败仍可导出。"]
    if result.get("reason"):
        rows.extend(["", REASONS.get(result["reason"], f"处理原因：{result['reason']}")])
    if result.get("message"):
        rows.append(result["message"])
    rows.extend(
        [
            "",
            f"累计处理 {usage['runs']} 轮；可观测模型尝试 {usage['observable_attempts']} 次。",
            f"累计计入额度 {usage['charged_tokens']:,} token；"
            f"用量不明 {usage['unknown_usage']} 次。",
        ]
    )
    local = value.get("local_ocr", {})
    if local.get("attempts"):
        rows.append(
            f"本地 OCR：{local['attempts']} 次执行，{local['regions']} 个区域，"
            f"预留输出 {local['reserved_output_tokens']:,} token。"
        )
    for job in value.get("cloud_jobs", []):
        rows.append(
            f"云 OCR 原文第 {job['page']} 页：{job['state']}；"
            f"提交 {job['submissions']} 次，请求 {job['requests']} 次。"
        )
    navigation = value.get("navigation_usage", {})
    if navigation.get("runs"):
        rows.append(
            f"导航累计：{navigation['observable_attempts']} 次模型尝试，"
            f"计入 {navigation['charged_tokens']:,} token；"
            f"用量不明 {navigation['unknown_usage']} 次。"
        )
        if not navigation["accounting_complete"]:
            rows.append("部分导航执行未正常结束，已知消耗保留，最终用量无法确认。")
    warnings = result.get("warnings", result.get("auxiliary_warnings"))
    if warnings:
        rows.extend(["", "辅助告警：", *warnings])
    rows.extend(["", f"资料版本：{value['source']['id']}"])
    return "\n".join(rows)


def evidence_text(value):
    rows = [position_text(value.location), ""]
    if value.context:
        rows.extend(["相关上下文：", value.context, "", "当前片段："])
    rows.append(value.text)
    if value.assets:
        rows.extend(
            ["", f"此片段关联 {len(value.assets)} 个已保存图像；PDF 可在“逐页检查”中查看。"]
        )
    if value.next_start is not None:
        rows.extend(["", "本次已读取部分内容。点击“继续读取当前片段”查看后续内容。"])
    return "\n".join(rows)
