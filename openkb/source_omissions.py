"""Keep local parsing omissions explicit while compiling usable document content."""

import re


def local_omissions(source, parsed):
    """A saved parsing diagnostic is a content omission, independent of format.

    Execution exceptions and immutable-artifact validation are handled by their
    owners; this function does not catch or downgrade either of them.
    """
    return [row["reason"] for row in parsed.quality if row["status"] == "needs_review"]


def has_readable_content(store, parsed):
    transcribed = {
        asset
        for row in parsed.quality
        if row["status"] == "verified"
        for asset in row.get("transcriptions", [])
    }
    for block in parsed.blocks:
        if block.kind == "image" and not transcribed.intersection(block.assets):
            continue
        text = store.asset(block.blob).read_text(encoding="utf-8")
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"!?\[[^\]]*\]\(asset:[0-9a-f]{64}\)", "", text)
        text = re.sub(
            r"\[(?:Original image unavailable|Embedded document (?:could not be parsed|"
            r"has no readable content)[^\]]*|"
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
