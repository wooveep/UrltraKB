"""Select OCR explicitly; policy is resolved before loading any optional runtime."""

import os

from openkb.processing import ProcessingIncomplete, processing_checkpoint


def default_local_profile(settings):
    if settings.policy == "off" or settings.backend != "system" or os.name != "nt":
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
    if settings.policy == "off":
        return None
    return LazyOcr(store, source, settings, native_profile=native_profile, retries=retries)


def _create_backend(store, source, settings, *, native_profile=None, retries=None):
    if settings.backend == "local" and settings.execution == "service":
        from openkb.ocr.service import PaddleService

        return (
            PaddleService(store, source, settings.service)
            if settings.service
            else UnavailableOcr("ocr_service_not_configured:open_ocr_settings")
        )
    if settings.backend == "local" and settings.installation:
        from openkb.ocr.installations import local_settings

        try:
            settings = settings.model_copy(update={"local": local_settings(settings.installation)})
        except (OSError, ValueError):
            return UnavailableOcr("ocr_runtime_not_ready:open_ocr_settings")
    if settings.backend == "cloud" and settings.cloud is not None:
        from openkb.ocr.cloud import CloudJobs

        return CloudJobs(store, source, settings.cloud, retries=retries)
    if settings.backend == "local" and settings.local is not None:
        from openkb.ocr.local import LocalOcr

        local = settings.local
        if settings.gpu_device:
            local = local.model_copy(update={"gpu_device": settings.gpu_device})
        return LocalOcr(store, source, local, retries=retries, device=settings.device)
    if settings.backend == "system":
        native_profile = default_local_profile(settings)
    if native_profile is not None:
        from openkb.ocr.windows import WindowsOcr

        return WindowsOcr(store, source, retries=retries, profile=native_profile)
    return UnavailableOcr(
        "system_ocr_unavailable:open_ocr_settings"
        if settings.backend == "system"
        else "ocr_not_configured:open_ocr_settings"
    )


class UnavailableOcr:
    def __init__(self, reason):
        self.reason = reason

    def page(self, *args, **kwargs):
        return [], self.reason

    def close(self):
        pass


class LazyOcr:
    """Native extraction decides whether an engine is needed before any runtime work."""

    def __init__(self, store, source, settings, **options):
        self.store, self.source, self.settings, self.options = store, source, settings, options
        self.backend = None

    def page(self, *args, **kwargs):
        if self.backend is None:
            self.backend = _create_backend(self.store, self.source, self.settings, **self.options)
        return self.backend.page(*args, **kwargs)

    def close(self):
        if self.backend is not None:
            self.backend.close()

    def cached_page(self, *args, **kwargs):
        if self.backend is not None and hasattr(self.backend, "cached_page"):
            return self.backend.cached_page(*args, **kwargs)
        return None


class PageOcr:
    def __init__(self, default, overrides):
        self.default, self.overrides = default, overrides

    def page(self, document, page, **kwargs):
        from openkb.ocr.optional import recognize

        return recognize(self.overrides.get(page, self.default), document, page, **kwargs)

    def close(self):
        for backend in [self.default, *self.overrides.values()]:
            if backend is not None:
                backend.close()
