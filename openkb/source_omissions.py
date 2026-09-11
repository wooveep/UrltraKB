"""Keep local parsing omissions explicit while compiling usable document content."""

import re


def local_omissions(source, parsed):
    """Return bounded source-content diagnostics, never waive unknown global failures."""
    pending = [row for row in parsed.quality if row["status"] == "needs_review"]
    if source.suffix != ".docx" or not pending:
        return []
    prefixes = (
        "docx_attachment:",
        "docx_attachment_",
        "docx_ole_",
        "docx_linked_object_",
        "docx_external_attachment_",
        "docx_conversion_warning:",
        "docx_part_unparsed:",
    )
    local = {
        "docx_image_asset_missing",
        "docx_image_requires_ocr",
        "unresolved_docx_note",
        "unresolved_docx_comment",
    }
    if any(
        row["reason"] not in local and not row["reason"].startswith(prefixes) for row in pending
    ):
        return []
    return [row["reason"] for row in pending]


def has_readable_content(store, parsed):
    for block in parsed.blocks:
        if block.kind == "image":
            continue
        text = store.asset(block.blob).read_text(encoding="utf-8")
        text = re.sub(r"!?\[[^\]]*\]\(asset:[0-9a-f]{64}\)", "", text)
        text = re.sub(
            r"\[(?:Original image unavailable|Embedded document could not be parsed[^\]]*|"
            r"Skipped non-document attachment:[^\]]*|unresolved comment|"
            r"(?:footnote|endnote) [^\]]*: unresolved)\]",
            "",
            text,
        )
        if text.strip():
            return True
    return False


def omission_notice(source, parsed):
    reasons = local_omissions(source, parsed)
    if not reasons:
        return ""
    return (
        "\n\n> 内容完整性提示：部分内容无法解析，已跳过或保留为未识别的原件。"
        "本页仅依据可用内容生成，未识别部分不代表没有信息。\n\n"
        + "\n".join("- `" + reason.replace("`", "'").replace("\n", " ") + "`" for reason in reasons)
    )
