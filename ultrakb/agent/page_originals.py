"""Frozen original evidence and the message view for a page-generation batch."""

import json
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class PageOriginalContext:
    message: dict
    has_original: bool
    has_current_original: bool = False
    windows: tuple[dict, ...] = ()

    def for_windows(self, windows: list[dict]) -> dict:
        """Render one selected batch with exactly its text and figure occurrence records."""
        old = json.dumps(list(self.windows), ensure_ascii=False)
        new = json.dumps(windows, ensure_ascii=False)
        blocks = [
            {**block, "text": block["text"].replace(old, new)}
            for block in self.message["content"]
            if block.get("type") == "text"
        ]
        return {**self.message, "content": blocks}

    def require_update(self, page: str) -> None:
        if not self.has_current_original:
            from ultrakb.compilation_report import CompilationIncomplete, report_compile_issue

            code = "page_update_originals_unread"
            report_compile_issue(code, page)
            raise CompilationIncomplete(
                (code,), (page,), "Existing page retained; locate relevant current originals first"
            )

    def navigation_page(self, title: str, doc_name: str) -> tuple[str, str]:
        return (
            f"Navigation: {title}; original details have not been read",
            f"# {title}\n\nNavigation entry for [[summaries/{doc_name}]]. "
            "No relevant original window was located for this page. "
            "Read the original source before using specific values or operating rules.\n",
        )


def bounded_short_originals(originals, reader):
    """Expose the complete short original as qualified, partitionable source windows."""
    current = reader.current_original_evidence()
    if current is None:
        return originals
    current = {
        **current,
        "source_role": "current",
        "selection_offset": 0,
        "selection_length": len(current["content"]),
    }
    selected = [current, *[w for w in originals.windows if w["path"] != current["path"]]]
    message = originals.for_windows(selected)
    for block in message["content"]:
        block["text"] = (
            block["text"]
            .replace(
                "Current short-document original is supplied in full in its dedicated "
                "original message. The windows below supplement it with linked sources.",
                "Current original evidence is partitioned in the frozen windows below.",
            )
            .replace("An empty supplementary list does not remove the current full original. ", "")
        )
    return replace(
        originals,
        message=message,
        windows=tuple(selected),
        has_original=True,
        has_current_original=True,
    )
