"""Contain optional OCR failures without swallowing document cancellation or deadlines."""

from openkb.evidence import BlockDraft
from openkb.processing import ProcessingIncomplete, processing_checkpoint


def recognize(backend, document, page, **kwargs):
    processing_checkpoint()
    if backend is None:
        return [], "ocr_unavailable"
    try:
        blocks, reason = backend.page(document, page, **kwargs)
        if not isinstance(blocks, list) or not all(isinstance(b, BlockDraft) for b in blocks):
            return [], "ocr_invalid_result"
        if reason is not None and not isinstance(reason, str):
            return [], "ocr_invalid_result"
        return blocks, reason
    except ProcessingIncomplete:
        raise
    except Exception as exc:
        processing_checkpoint()
        # Provider exceptions can contain credentials or signed download URLs.
        return [], "ocr_backend_failed:" + type(exc).__name__
