"""Cheap visual admission checks; rejected candidates still retain their images."""

from __future__ import annotations


def ocr_candidate(image) -> bool:
    """Exclude tiny icons, narrow toolbars and flat fills, without claiming OCR accuracy."""
    if min(image.size) <= 48 or max(image.size) < 160:
        return False
    low, high = image.convert("L").getextrema()
    return high - low >= 24


def decorative_path(path, page_rect) -> bool:
    """Recognize full-page flat backgrounds and simple rules at the page edge only."""
    import pymupdf

    items = path["items"]
    rect = path["rect"]
    if len(items) == 1 and items[0][0] == "re" and path.get("fill") is not None:
        # A large box inside the content area could be a meaningful diagram.
        if pymupdf.Rect(rect).contains(page_rect + (2, 2, -2, -2)):
            return True
    return bool(
        items
        and path.get("fill") is None
        and path.get("width", 1) <= 2
        and all(item[0] == "l" and item[1].y == item[2].y for item in items)
        and (rect.y1 <= page_rect.y0 + 30 or rect.y0 >= page_rect.y1 - 30)
    )
