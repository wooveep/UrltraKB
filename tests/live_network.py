"""Only admitted, fingerprint-matched model sends may leave a pytest process."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

_PERMIT = ContextVar("real_llm_test_send", default=None)
_INSTALLED = False


@contextmanager
def admitted_transport(client, budget, identifier, fingerprint):
    token = _PERMIT.set((client, budget, identifier, fingerprint))
    try:
        yield
    finally:
        _PERMIT.reset(token)


def _check(client, request):
    if request.url.host in {"127.0.0.1", "localhost", "::1", "testserver"}:
        return
    if request.method != "POST" or not request.url.path.rstrip("/").endswith(
        (
            "/completions",
            "/responses",
            "/messages",
            "/generateContent",
            ":generateContent",
            ":streamGenerateContent",
        )
    ):
        return
    permit = _PERMIT.get()
    if permit is None or permit[0] is not client:
        raise RuntimeError("Unaccounted model transport: use the shared real LLM test fixture")
    payload = json.loads(request.content)
    actual = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        request.url.scheme != "https"
        or request.url.host != "api.deepseek.com"
        or request.url.port not in (None, 443)
        or payload.get("model") != "deepseek-flash"
        or actual != permit[3]
    ):
        raise RuntimeError("The outgoing model request differs from its cost reservation")
    permit[1].mark_sent(permit[2], actual)


def install_network_guard():
    global _INSTALLED
    if _INSTALLED:
        return
    original_send, original_asend = httpx.Client.send, httpx.AsyncClient.send

    def send(client, request, *args, **kwargs):
        _check(client, request)
        return original_send(client, request, *args, **kwargs)

    async def asend(client, request, *args, **kwargs):
        _check(client, request)
        return await original_asend(client, request, *args, **kwargs)

    httpx.Client.send, httpx.AsyncClient.send = send, asend
    _INSTALLED = True
