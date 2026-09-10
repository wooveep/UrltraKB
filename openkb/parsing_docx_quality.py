"""Separate preserved formatting from genuinely unsupported DOCX content."""

import re

_FORMATTING_ELEMENTS = {
    "w:tblPrEx",
    "{urn:schemas-microsoft-com:office:office}lock",
    "office-word:anchorlock",
}


def conversion_quality(message: str) -> dict[str, str]:
    # Mammoth keeps run/paragraph text even when its HTML style map has no match.
    formatting = message.startswith(("Unrecognised paragraph style:", "Unrecognised run style:"))
    ignored = message.removeprefix("An unrecognised element was ignored: ")
    formatting = formatting or ignored in _FORMATTING_ELEMENTS
    # The image node and its original bytes are retained by our image callback.
    formatting = formatting or bool(
        re.fullmatch(r"Image of type image/[^\s]+ is unlikely to display in web browsers", message)
    )
    return {
        "status": "verified" if formatting else "needs_review",
        "reason": "docx_conversion_warning:" + message,
    }
