"""Frozen original evidence and the message view for a page-generation batch."""

import json
from dataclasses import dataclass


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
