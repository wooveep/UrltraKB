"""Task-local redaction for SDK diagnostics produced during connection testing."""

import logging
import traceback
from contextlib import contextmanager
from contextvars import ContextVar

_SECRETS: ContextVar[tuple[str, ...]] = ContextVar("model_log_secrets", default=())


class _Redact(logging.Filter):
    def filter(self, record):
        values = _SECRETS.get()
        if not values:
            return True

        def redact(text):
            for secret in values:
                text = text.replace(secret, "[凭证已隐藏]")
            return text

        record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


_FILTER = _Redact()


@contextmanager
def redact_model_logs(credentials):
    # The filter lives on the SDK loggers. Its context is confined to this
    # operation and its asyncio children, so concurrent tasks keep their scope.
    for name in ("openai.agents", "LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        logging.getLogger(name).addFilter(_FILTER)
    values = tuple(
        value for value in (credentials.api_key, *credentials.extra_headers.values()) if value
    )
    token = _SECRETS.set(values)
    try:
        yield
    finally:
        _SECRETS.reset(token)
