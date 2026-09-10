"""Stop optional image OCR after a document-family resource limit is reached."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from openkb.ocr.optional import recognize

_STOP_REASONS = {
    "ocr_time_budget_exhausted",
    "ocr_request_budget_exhausted",
    "ocr_page_budget_exhausted",
    "ocr_download_budget_exhausted",
    "ocr_recognition_budget_exhausted",
    "cloud_credentials_missing",
    "cloud_daily_quota_exhausted",
    "cloud_rate_limited",
    "cloud_http_401",
    "cloud_http_403",
}


@dataclass
class _State:
    reason: str | None = None


_current: ContextVar[_State | None] = ContextVar("optional_image_ocr", default=None)


class ImageOcrSession:
    def __init__(self, backend, state):
        self.backend, self.state = backend, state
        self.skipped = 0

    def skip_optional(self) -> bool:
        if self.state.reason and not callable(getattr(self.backend, "cached_page", None)):
            self.skipped += 1
            return True
        return False

    def page(self, *args, **kwargs):
        if self.state.reason:
            cached = getattr(self.backend, "cached_page", None)
            result = cached(*args, **kwargs) if callable(cached) else None
            if result is not None:
                return result
            self.skipped += 1
            return [], "ocr_optional_image_skipped"
        blocks, reason = recognize(self.backend, *args, **kwargs)
        if reason in _STOP_REASONS or (reason and reason.startswith("ocr_backend_failed:")):
            self.state.reason = reason
        return blocks, reason

    def notices(self) -> list[dict]:
        if not self.skipped:
            return []
        return [
            {
                "status": "verified",
                "reason": "docx_image_ocr_notice:remaining_frames_skipped:"
                f"{self.skipped}:{self.state.reason}",
            }
        ]


@contextmanager
def image_ocr_scope(backend):
    """Share exhaustion only with nested document attachments; reset even on cancellation."""
    token = _current.set(_current.get() or _State())
    try:
        yield ImageOcrSession(backend, _current.get()) if backend is not None else None
    finally:
        _current.reset(token)
