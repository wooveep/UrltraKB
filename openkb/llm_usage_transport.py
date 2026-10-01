"""Observe HTTPX sends inside a single import call, including SDK retries.

The process hooks are inert outside the ContextVar scope. Each actual send gets
its own request ID before the network operation. Providers using another send
library retain call-level records marked collection_complete=False.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from openkb.llm_usage import _SCOPE, active_scope, begin_request, finish_request


@dataclass
class ModelCall:
    scope: object
    model: str
    stage: str
    first_id: str
    sent_ids: list[str] = field(default_factory=list)

    def sending(self):
        identity = (
            self.first_id
            if not self.sent_ids
            else begin_request(self.model, self.stage, scope=self.scope)
        )
        self.sent_ids.append(identity)
        return identity

    def finish(self, response=None, *, state="completed"):
        # A consumed HTTP response is authoritative. SDKs can synthesize zero
        # usage when the provider omitted it; that must remain unknown here.
        finish_request(
            self.sent_ids[-1] if self.sent_ids else self.first_id,
            None if self.sent_ids else response,
            state=state,
            scope=self.scope,
        )


_CALL: ContextVar[ModelCall | None] = ContextVar("openkb_usage_model_call", default=None)
_GUARD = threading.Lock()
_INSTALLED = False


def _response(response):
    try:
        return response.json() if response.is_stream_consumed else None
    except (ValueError, UnicodeError):
        return None


def _model_request(request):
    import json

    try:
        value = json.loads(request.content)
        return isinstance(value, dict) and any(
            key in value for key in ("messages", "input", "prompt", "contents")
        )
    except (ValueError, UnicodeError):
        return False


def _failure_state(exc):
    import asyncio

    from openkb.locks import LockCancelled

    return (
        "cancelled"
        if isinstance(exc, (asyncio.CancelledError, LockCancelled, KeyboardInterrupt))
        else "failed"
    )


def install_send_observer():
    global _INSTALLED
    with _GUARD:
        if _INSTALLED:
            return
        import httpx

        sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

        def send(client, request, *args, **kwargs):
            call = _CALL.get()
            if call is None or request.method != "POST" or not _model_request(request):
                return sync_send(client, request, *args, **kwargs)
            identity = call.sending()
            try:
                response = sync_send(client, request, *args, **kwargs)
            except BaseException as exc:
                finish_request(
                    identity, state=_failure_state(exc), scope=call.scope, observation="transport"
                )
                raise
            finish_request(
                identity,
                _response(response),
                state="completed" if response.is_success else "failed",
                scope=call.scope,
                observation="transport",
            )
            return response

        async def asend(client, request, *args, **kwargs):
            call = _CALL.get()
            if call is None or request.method != "POST" or not _model_request(request):
                return await async_send(client, request, *args, **kwargs)
            identity = call.sending()
            try:
                response = await async_send(client, request, *args, **kwargs)
            except BaseException as exc:
                finish_request(
                    identity, state=_failure_state(exc), scope=call.scope, observation="transport"
                )
                raise
            finish_request(
                identity,
                _response(response),
                state="completed" if response.is_success else "failed",
                scope=call.scope,
                observation="transport",
            )
            return response

        httpx.Client.send, httpx.AsyncClient.send = send, asend
        _INSTALLED = True


@contextmanager
def observe_model_call(model: str, stage: str):
    scope = active_scope()
    if scope is None:
        yield None
        return
    install_send_observer()
    identity = begin_request(model, stage)
    assert identity is not None
    call = ModelCall(scope, model, stage, identity)
    token = _CALL.set(call)
    try:
        yield call
    except BaseException as exc:
        call.finish(state=_failure_state(exc))
        raise
    finally:
        _CALL.reset(token)


class IndexUsageObserver:
    def __init__(self):
        self.scope = active_scope()

    @contextmanager
    def call(self, model, stage):
        token = _SCOPE.set(self.scope)
        try:
            with observe_model_call(model, stage) as call:
                yield call
        finally:
            _SCOPE.reset(token)
