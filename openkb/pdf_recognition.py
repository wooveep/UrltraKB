"""PDF text recognition boundary. The only shipped recognizer reads the text layer.

A future recognizer can implement PageRecognizer without changing admission or
quality checks. No OCR, opacity filtering, or character substitution is enabled.
"""

from pathlib import Path
from typing import Protocol

import pymupdf

PDF_RECOGNITION_POLICY = "pdf-text-layer-v2"


class PdfRecognitionError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("PDF导入识别异常")


class PageRecognizer(Protocol):
    def __call__(self, page: pymupdf.Page) -> str: ...


def text_layer(page: pymupdf.Page) -> str:
    return page.get_text()


def recognize_pdf(
    path: Path, *, recognize_page: PageRecognizer = text_layer, allow_empty: bool = False
) -> list[tuple[str, str]]:
    """Preserve physical ordinals and hidden text; reject unreadable whole documents.

    Blank pages within a readable document remain valid. Results from any future
    recognizer still have to pass the caller's text quality gate before admission.
    """
    try:
        with pymupdf.open(path) as pdf:
            parts = [(f"page[{i}]", recognize_page(page)) for i, page in enumerate(pdf, 1)]
    except (RuntimeError, ValueError, OSError) as error:
        raise PdfRecognitionError(f"parser_error:{type(error).__name__}") from error
    if not allow_empty and not any(text.strip() for _, text in parts):
        raise PdfRecognitionError("no_readable_text")
    return parts
