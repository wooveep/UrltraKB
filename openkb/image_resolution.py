"""Shared admission policy for recognizing source-image pixels, not display DPI."""


def low_resolution(size: tuple[int, int]) -> bool:
    """Retain small images but do not spend OCR or vision requests on them."""
    width, height = size
    return max(width, height) < 256 or width * height < 16_000
