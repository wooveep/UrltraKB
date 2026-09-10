"""Choose the requested local/cloud OCR, with installed Windows OCR as the local default."""

import os

from openkb.processing import ProcessingIncomplete, processing_checkpoint


def default_local_profile(settings):
    if settings.backend != "local" or settings.local is not None or os.name != "nt":
        return None
    from openkb.ocr.windows import runtime_profile

    try:
        return dict(runtime_profile())
    except (OSError, ValueError):
        return None
    except ProcessingIncomplete as exc:
        if exc.reason != "windows_ocr_timeout":
            raise
        processing_checkpoint()
        return None


def create_ocr(store, source, settings, *, native_profile=None, retries=None):
    if settings.backend == "cloud" and settings.cloud is not None:
        from openkb.ocr.cloud import CloudJobs

        return CloudJobs(store, source, settings.cloud, retries=retries)
    if settings.backend == "local" and settings.local is not None:
        from openkb.ocr.local import LocalOcr

        return LocalOcr(store, source, settings.local, retries=retries)
    if native_profile is not None:
        from openkb.ocr.windows import WindowsOcr

        return WindowsOcr(store, source, retries=retries)
    return None
