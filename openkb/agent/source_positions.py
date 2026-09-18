"""Keep original section locations visible without inventing applicability claims."""

import re


def _literal(value):
    text = " ".join(str(value).splitlines())
    return re.sub(r"([\\`*_{}\[\]<>#])", r"\\\1", text)


def with_source_position(content, scope, language="en"):
    if not content.strip():
        return content  # Secondary details alone do not create a knowledge claim.
    chinese = language.lower().startswith("zh")
    lines = []
    for position in scope.get("positions", []):
        headings = position["headings"]
        attached = position["role"] == "attachment"
        if not headings and not attached:
            continue
        label = (
            ("附件 " if chinese else "attachment ") + _literal(position["name"])
            if attached
            else ("主资料" if chinese else "enclosing document")
        )
        path = " › ".join(_literal(heading) for heading in headings)
        lines.append(label + (": " + path if path else ""))
    if not lines:
        return content
    label = "原文章节位置" if chinese else "Original section location"
    prefix = "> " + label + " — " + "\n> ".join(lines) + "\n\n"
    # Keep a model's leading title in place for title-only corrections. The
    # exact source-bound prefix is idempotent across correction and restoration.
    heading = re.match(r"\A#{1,6}[^\n]*\n(?:[ \t]*\n)*", content)
    at = heading.end() if heading else 0
    if content[at:].startswith(prefix):
        return content
    return content[:at] + prefix + content[at:]
